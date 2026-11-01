/*
 * The toolbar panel, inside the simulator: three tabs -- Comms, Flight,
 * Settings -- from the "WilcoATC Toolbar Panel" design.
 *
 * It contains no air traffic control at all. The phraseology, the voices, the
 * speech recognition and the transcript all live in the application; this
 * polls it, redraws, and sends a handful of commands back. That is
 * deliberate: any rule duplicated here is a rule that will one day disagree
 * with the one the radio is actually using. The one table it does carry --
 * the quick replies -- is the desktop window's, word for word, and a test
 * holds the two together. They stay English whatever the panel is read in:
 * they are transmissions, and the radio is English.
 *
 * Its words are the program's: the language set in WilcoATC (ui.language)
 * and the panel's own catalogue for it, which the application serves
 * (/i18n/<code>.json, the "tb.*" keys). The English written beside each key
 * here is what shows until that arrives, or if it never does.
 *
 * XMLHttpRequest rather than fetch, and ES5 throughout: the panel runs in
 * Coherent GT, whose embedded version moves between simulator builds, and
 * the failure of a newer feature is a blank panel with no message in it.
 *
 * The application serves its window on a port the operating system picks,
 * because nothing else has to find it. This does have to find it, so it is
 * given a fixed one -- toolbar.port in config.yaml, 8787 unless changed.
 */

(function () {
    'use strict';

    // Must match toolbar.port in the application's config.yaml.
    var PORT = 8787;
    var BASE = 'http://127.0.0.1:' + PORT;

    // Twice a second. Fast enough that keying the microphone looks immediate,
    // slow enough to be invisible on the loopback interface.
    var PERIOD = 500;

    // The veil only comes down after two consecutive failures. One request
    // lost while a scene loads would otherwise flash "not running" across a
    // link that is perfectly healthy.
    var FAILS_BEFORE_VEIL = 2;

    // How many transmissions the conversation shows.
    var LINES = 5;

    // The field's stations are asked for again this often while the Flight
    // tab is open: they change when the field does, not every half second.
    var STATIONS_EVERY = 15000;

    // The phrases a pilot needs next, by phase of flight. The desktop
    // window's PHRASES in app.js, exactly: each one is a sentence the intent
    // parser already understands, and tests/test_toolbar.py fails if the two
    // tables ever differ.
    var PHRASES = {
        preflight: ['request clearance', 'request pushback', 'radio check'],
        cleared: ['ready to taxi', 'request pushback'],
        pushback: ['ready to taxi'],
        taxi_out: ['holding short, ready for departure', 'say again'],
        holding_short: ['holding short, ready for departure'],
        lined_up: ['ready for departure'],
        takeoff: ['with you, climbing', 'request climb'],
        departure: ['with you, climbing', 'request climb', 'request direct'],
        enroute: ['request climb', 'request descent', 'request direct'],
        descent: ['request descent', 'request approach', 'field in sight'],
        approach: ['request approach', 'field in sight', 'going around'],
        landing: ['going around', 'request taxi to parking'],
        landed: ['request taxi to parking'],
        taxi_in: ['request taxi to parking']
    };

    var PHASE_NAMES = {
        preflight: 'Preflight', cleared: 'Cleared', pushback: 'Pushback',
        taxi_out: 'Taxi', holding_short: 'Holding', lined_up: 'Lined up',
        takeoff: 'Takeoff', departure: 'Departure', enroute: 'Cruise',
        descent: 'Descent', approach: 'Approach', landing: 'Final',
        landed: 'Landed', taxi_in: 'Taxi in', parked: 'Parked'
    };

    // The controllers a flight meets, in the order it meets them, by the
    // position the application files each station under.
    var POSITIONS = [
        ['DEL', 'tb.pos_del', 'Clearance'],
        ['GND', 'tb.pos_gnd', 'Ground'],
        ['TWR', 'tb.pos_twr', 'Tower'],
        ['DEP', 'tb.pos_dep', 'Departure'],
        ['APP', 'tb.pos_app', 'Approach'],
        ['CTR', 'tb.pos_ctr', 'Centre']
    ];
    // After these phases the stations that matter are the destination's.
    var ARRIVING = { descent: 1, approach: 1, landing: 1, landed: 1, taxi_in: 1, parked: 1 };

    // The settings worth changing from the cockpit, each a config path.
    var TOGGLES = [
        ['audio.radio_effects', 'tb.opt_radio', 'Radio effects', 'tb.opt_radio_sub', 'The VHF sound on every controller'],
        ['immersion.enabled', 'tb.opt_crew', 'First officer', 'tb.opt_crew_sub', 'Callouts, checklists and the crew in the cabin'],
        ['ui.landing_report', 'tb.opt_landing', 'Landing report', 'tb.opt_landing_sub', 'The touchdown card after landing']
    ];
    var VOLUME_MAX = 1.5;           // audio.volume's own range

    // Key names as the keyboard library gives them, as a key cap shows them.
    var KEY_CAPS = { grave: String.fromCharCode(96), space: 'SPACE', caps_lock: 'CAPS', ctrl_r: 'RCTRL',
                     ctrl_l: 'LCTRL', alt_r: 'RALT', alt_l: 'LALT', shift_r: 'RSHIFT',
                     shift_l: 'LSHIFT', tab: 'TAB' };

    var el = {};
    var S = {};                     // the catalogue, once it has arrived
    var tab = 'comms';
    var fails = 0;
    var keyed = false;
    var focused = false;
    var sending = false;
    // The finger is on the microphone. While it is, that is the truth: a
    // poll sent a moment before the press comes back saying nobody is
    // transmitting, and believing it switched the key off under the finger.
    var holding = false;
    var lastState = {};
    var standby = 0;
    var timer = null;
    var lastFeed = '';
    var lastPhase = null;
    var stationsFor = '';
    var stationsAt = 0;
    var stations = [];
    var settings = null;
    var settingsAsked = 0;
    var landingAt = 0;
    var landingTimer = null;
    var LANDING_SHOWN_MS = 60000;

    function $(id) { return document.getElementById(id); }

    function request(method, path, body, onOk, onFail) {
        var xhr = new XMLHttpRequest();
        try {
            xhr.open(method, BASE + path
                + (method === 'GET'
                    ? (path.indexOf('?') < 0 ? '?' : '&') + 't=' + Date.now()
                    : ''), true);
            xhr.timeout = 3000;
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function () {
                if (xhr.readyState !== 4) return;
                if (xhr.status >= 200 && xhr.status < 300) {
                    var data = null;
                    try { data = JSON.parse(xhr.responseText); } catch (e) { }
                    if (onOk) onOk(data);
                } else if (onFail) { onFail(xhr.status); }
            };
            xhr.ontimeout = function () { if (onFail) onFail(0); };
            xhr.onerror = function () { if (onFail) onFail(0); };
            // Always a body on a POST: the application's handlers read one.
            xhr.send(method === 'POST' ? JSON.stringify(body || {}) : null);
        } catch (e) {
            if (onFail) onFail(-1);
        }
    }

    // ------------------------------------------------------------ words ---

    function t(key, english, params) {
        var said = S[key] !== undefined && S[key] !== '' ? S[key] : english;
        if (params) {
            for (var name in params) {
                if (params.hasOwnProperty(name)) {
                    said = said.split('{' + name + '}').join(String(params[name]));
                }
            }
        }
        return said;
    }

    // The English in the markup, captured once, so a key the catalogue lacks
    // keeps its English rather than showing the key.
    var english = {};

    function paintWords() {
        var nodes = document.querySelectorAll('[data-t]');
        for (var i = 0; i < nodes.length; i++) {
            var key = nodes[i].getAttribute('data-t');
            if (english[key] === undefined) english[key] = nodes[i].textContent;
            nodes[i].textContent = t(key, english[key]);
        }
        el.input.setAttribute('placeholder', t('tb.type_here', 'Type your message…'));
        el.swap.title = t('tb.switch_why', 'Swap active and standby, in the aircraft too');
        el.atis.title = t('tb.atis_why', 'Play the ATIS');
        el.send.title = t('tb.send', 'Send');
        lastFeed = '';
        if (settings) paintSettings();
        paintStations();
    }

    // The language the window is read in: ui.language, or the simulator's
    // own when that is "auto" -- the window takes it from Windows, which the
    // simulator's interface usually is too.
    function codeFor(language) {
        if (language && language !== 'auto') return language;
        var nav = (navigator.language || 'en').replace('_', '-');
        var lang = nav.split('-')[0].toLowerCase();
        var region = (nav.split('-')[1] || '').toUpperCase();
        if (lang === 'zh') return region === 'TW' || region === 'HK' ? 'zh-Hant' : 'zh-Hans';
        if (lang === 'no' || lang === 'nn') return 'nb';
        return lang;
    }

    function loadWords(language) {
        var code = codeFor(language);
        if (code === S.__code) return;
        if (code === 'en') {
            S = { __code: 'en' };
            paintWords();
            return;
        }
        request('GET', '/i18n/' + code + '.json', null, function (table) {
            if (!table) return;
            table.__code = code;
            S = table;
            paintWords();
        });
    }

    // ------------------------------------------------------------ drawing ---

    function text(node, value) {
        if (node && node.textContent !== value) node.textContent = value;
    }

    function cls(node, value) {
        if (node && node.className !== value) node.className = value;
    }

    function mhz(value) {
        return value ? Number(value).toFixed(3) : '---.---';
    }

    // By hand: toLocaleString in Coherent GT has not always known a locale.
    function thousands(n) {
        return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    function three(n) {
        n = Math.round(n) % 360;
        if (n <= 0) n += 360;
        return (n < 10 ? '00' : n < 100 ? '0' : '') + n;
    }

    function sameChannel(a, b) {
        return a && b && Math.abs(Number(a) - Number(b)) < 0.004;
    }

    function paintLink(state) {
        var sim = !!state.connected;
        cls(el.link, 'wa-link' + (sim ? '' : ' is-nosim'));
        text(el.linkText, sim ? t('tb.linked', 'LINKED') : t('tb.no_sim', 'NO SIM'));
    }

    function paintRadio(state) {
        var radio = state.radio || {};
        var tuned = radio.com1 || 0;
        text(el.freq, mhz(tuned));
        cls(el.freq, 'wa-freq' + (tuned && radio.powered !== false ? '' : ' is-empty'));

        // Who answers, and how far away: the name in full strength, the
        // distance after it quieter, because it qualifies the name.
        var station = radio.station || '';
        // A cold and dark aeroplane: the radio has no power, and says so
        // rather than showing a station nobody can hear.
        var dead = radio.powered === false;
        if (dead) station = '';
        var line = dead ? t('tb.no_power', 'Radio off — no power') : station || (tuned ? t('tb.nobody', 'Nobody on this frequency')
            : (state.connected ? t('tb.nothing_tuned', 'Nothing tuned')
                : t('tb.no_aircraft', 'No aircraft')));
        var away = station && radio.distance_nm
            ? '  ' + Number(radio.distance_nm).toFixed(1) + ' nm' : '';
        if (el.station.getAttribute('data-line') !== line + away) {
            el.station.setAttribute('data-line', line + away);
            el.station.innerHTML = '';
            el.station.appendChild(document.createTextNode(line));
            if (away) {
                var small = document.createElement('small');
                small.textContent = away;
                el.station.appendChild(small);
            }
        }

        keyed = holding || !!state.transmitting;
        cls(el.dot, 'wa-dot' + (keyed ? ' is-live' : station ? ' is-on' : ''));

        standby = radio.com1_standby || 0;
        text(el.standbyName, radio.standby_station ? radio.standby_station + ' ·' : '');
        text(el.standby, mhz(standby));
        cls(el.swap, 'wa-switch' + (standby ? '' : ' is-off'));
    }

    function paintStatus(state) {
        var flight = state.flight || {};
        var status = '';
        var kind = '';
        if (keyed) { status = t('tb.transmitting', 'Transmitting — release to send'); kind = ' is-live'; }
        else if (flight.pending) { status = t('tb.read_back', 'Read back — the controller is waiting'); kind = ' is-wait'; }
        else if (sending) { status = t('tb.sent', 'Sent'); kind = ' is-shown'; }
        else if (state.playing) { status = t('tb.speaking', 'Controller speaking'); kind = ' is-shown'; }
        text(el.status, status);
        cls(el.status, 'wa-status' + kind);
    }

    function paintReplies(phase) {
        if (phase === lastPhase) return;
        lastPhase = phase;
        var wanted = PHRASES[phase] || ['say again', 'radio check'];
        el.replies.innerHTML = '';
        for (var i = 0; i < wanted.length; i++) {
            el.replies.appendChild(reply(wanted[i]));
        }
    }

    function reply(phrase) {
        var node = document.createElement('div');
        node.className = 'wa-reply';
        node.textContent = phrase.charAt(0).toUpperCase() + phrase.slice(1);
        node.addEventListener('click', function () {
            request('POST', '/api/say', { text: phrase });
            // Acknowledged at once: the answer is a controller speaking, which
            // takes a second or two to arrive.
            node.className = 'wa-reply is-sent';
            setTimeout(function () { node.className = 'wa-reply'; }, 1500);
        });
        return node;
    }

    function paintPtt(state) {
        var key = state.ptt_key ? String(state.ptt_key).toLowerCase() : '';
        text(el.key, key ? (KEY_CAPS[key] || key.toUpperCase()) : '');
        var typed = !!el.input.value;
        cls(el.mic, 'wa-mic' + (keyed ? ' is-live' : state.playing ? ' is-receiving' : ''));
        cls(el.compose, 'wa-compose' + (focused ? ' is-focused' : ''));
        cls(el.send, 'wa-send' + (typed ? ' is-ready' : ''));
        el.mic.title = t('tb.hold_to_talk', 'Hold to talk');
    }

    /*
        The application's kinds of transmission, in the three weights this
        panel has room for. Naming them all rather than defaulting means a
        kind added later lands on 'traffic' -- present but quiet -- instead
        of in the strongest weight pretending to be an instruction.
    */
    function kindOf(kind) {
        if (kind === 'atc' || kind === 'atis') return 'atc';
        if (kind === 'pilot') return 'pilot';
        return 'traffic';
    }

    function whoSaid(line, kind) {
        if (kind === 'pilot') return t('tb.you', 'You');
        if (line.kind === 'atis') return 'ATIS';
        if (line.kind === 'crew') return line.speaker || t('tb.crew', 'Crew');
        return line.speaker || (kind === 'atc' ? 'ATC' : t('tb.traffic', 'Traffic'));
    }

    function paintFeed(said) {
        var lines = (said || []).slice(-LINES);
        var key = lines.map(function (line) { return line.at + line.text; }).join('|');
        if (key === lastFeed) return;
        lastFeed = key;
        el.feed.innerHTML = '';
        if (!lines.length) {
            var empty = document.createElement('div');
            empty.className = 'wa-empty';
            empty.textContent = t('tb.nothing_heard', 'Nothing heard yet');
            el.feed.appendChild(empty);
            return;
        }
        for (var i = 0; i < lines.length; i++) {
            var kind = kindOf(lines[i].kind);
            var msg = document.createElement('div');
            msg.className = 'wa-msg wa-msg-' + kind;
            if (kind !== 'traffic') {
                var who = document.createElement('div');
                who.className = 'wa-msg-who';
                who.textContent = whoSaid(lines[i], kind);
                msg.appendChild(who);
            }
            var body = document.createElement('div');
            body.className = 'wa-msg-text';
            body.textContent = kind === 'traffic' && lines[i].speaker
                ? lines[i].speaker + ': ' + lines[i].text : lines[i].text;
            msg.appendChild(body);
            el.feed.appendChild(msg);
        }
    }

    // ------------------------------------------------------------- flight ---

    function paintFlight(state) {
        var flight = state.flight || {};
        var aircraft = state.aircraft || {};
        text(el.callsign, aircraft.callsign || '–');
        var phase = flight.phase || '';
        var what = [];
        if (aircraft.type) what.push(aircraft.type);
        if (phase) what.push(flight.pending ? t('tb.read_back_short', 'Read back')
            : t('tb.phase_' + phase, PHASE_NAMES[phase] || phase));
        text(el.type, what.join(' · '));
        text(el.dep, flight.departure || '----');
        text(el.dest, flight.destination || '----');
        text(el.depName, flight.departure_name || '');
        text(el.destName, flight.destination_name || '');
        var known = flight.progress !== null && flight.progress !== undefined;
        cls(el.track, 'wa-track' + (known ? '' : ' is-unknown'));
        if (known) {
            var pct = Math.round(flight.progress * 1000) / 10 + '%';
            el.trackFill.style.width = pct;
            el.trackDot.style.left = pct;
        }

        text(el.rwy, flight.cleared_runway || flight.runway || '–');
        text(el.sid, flight.sid || '–');
        var position = state.position || {};
        text(el.squawk, flight.squawk || position.squawk || '–');
        text(el.atisLetter, state.atis_letter || '–');
        var wx = state.weather;
        if (wx) {
            text(el.qnh, String(wx.qnh_hpa));
            text(el.wind, wx.wind_kt < 1 ? t('tb.calm', 'Calm')
                : three(wx.wind_dir) + '/' + wx.wind_kt
                  + (wx.gust_kt > wx.wind_kt ? 'G' + wx.gust_kt : ''));
        } else {
            text(el.qnh, '–');
            text(el.wind, '–');
        }

        // Whose stations to list: the field being left until the descent,
        // the one being flown to after it, and with no plan the one here.
        var field = ARRIVING[phase] ? flight.destination : flight.departure;
        field = field || (state.radio || {}).here || '';
        var now = Date.now();
        if (tab === 'flight' && field
                && (field !== stationsFor || now - stationsAt > STATIONS_EVERY)) {
            stationsAt = now;
            request('GET', '/api/frequencies?ident=' + encodeURIComponent(field), null,
                function (data) {
                    stationsFor = field;
                    stations = (data && data.stations) || [];
                    paintStations();
                });
        }
        paintStations();
    }

    function paintStations() {
        if (!el.stations) return;
        var tuned = ((lastState.radio || {}).com1) || 0;
        var rows = [];
        for (var p = 0; p < POSITIONS.length; p++) {
            for (var s = 0; s < stations.length; s++) {
                if (stations[s].position === POSITIONS[p][0]) {
                    rows.push({ name: t(POSITIONS[p][1], POSITIONS[p][2]),
                                callsign: stations[s].callsign,
                                mhz: stations[s].mhz, carrier: stations[s].carrier });
                    break;
                }
            }
        }
        var key = rows.map(function (r) { return r.name + r.mhz; }).join('|') + '@' + tuned;
        if (el.stations.getAttribute('data-key') === key) return;
        el.stations.setAttribute('data-key', key);
        el.stations.innerHTML = '';
        if (!rows.length) {
            var empty = document.createElement('div');
            empty.className = 'wa-list-empty';
            empty.textContent = t('tb.no_stations', 'No published stations for this field');
            el.stations.appendChild(empty);
            return;
        }
        for (var i = 0; i < rows.length; i++) {
            el.stations.appendChild(stationRow(rows[i], sameChannel(rows[i].carrier, tuned)));
        }
    }

    function stationRow(row, on) {
        var node = document.createElement('div');
        node.className = 'wa-stop' + (on ? ' is-on' : ' is-tune');
        node.title = on ? row.callsign : t('tb.tune_standby', 'Put it in standby');
        var dot = document.createElement('span');
        dot.className = 'wa-stop-dot';
        var name = document.createElement('span');
        name.className = 'wa-stop-name';
        name.textContent = row.name;
        var freq = document.createElement('span');
        freq.className = 'wa-stop-freq';
        freq.textContent = row.mhz;
        node.appendChild(dot);
        node.appendChild(name);
        node.appendChild(freq);
        if (!on) {
            // Into standby, never straight onto the active: changing who you
            // are talking to stays a deliberate press of Switch.
            node.addEventListener('click', function () {
                request('POST', '/api/tune', { mhz: row.carrier, standby: true, radio: 1 }, tick);
            });
        }
        return node;
    }

    // ----------------------------------------------------------- settings ---

    function loadSettings() {
        settingsAsked = Date.now();
        request('GET', '/api/settings', null, function (data) {
            if (!data || !data.config) return;
            settings = data.config;
            loadWords((settings.ui || {}).language);
            paintSettings();
        });
    }

    function valueAt(path) {
        var parts = path.split('.');
        var node = settings;
        for (var i = 0; i < parts.length; i++) {
            if (!node) return undefined;
            node = node[parts[i]];
        }
        return node;
    }

    function save(path, value) {
        var body = {};
        body[path] = value;
        request('POST', '/api/settings', body, function (result) {
            if (result && result.config) settings = result.config;
            paintSettings();
        });
    }

    function paintSettings() {
        if (!settings) return;
        el.toggles.innerHTML = '';
        for (var i = 0; i < TOGGLES.length; i++) {
            el.toggles.appendChild(toggleRow(TOGGLES[i]));
        }
        var volume = Number(valueAt('audio.volume'));
        if (isNaN(volume)) volume = 1;
        paintVolume(volume);
    }

    function toggleRow(spec) {
        var on = !!valueAt(spec[0]);
        var node = document.createElement('div');
        node.className = 'wa-toggle' + (on ? ' is-on' : '');
        node.innerHTML = '<div class="wa-toggle-text"><div class="wa-toggle-label"></div>'
            + '<div class="wa-toggle-sub"></div></div>'
            + '<div class="wa-toggle-track"><div class="wa-toggle-knob"></div></div>';
        node.querySelector('.wa-toggle-label').textContent = t(spec[1], spec[2]);
        node.querySelector('.wa-toggle-sub').textContent = t(spec[3], spec[4]);
        node.addEventListener('click', function () {
            node.className = 'wa-toggle' + (on ? '' : ' is-on');
            save(spec[0], !on);
        });
        return node;
    }

    function paintVolume(volume) {
        var share = Math.max(0, Math.min(1, volume / VOLUME_MAX));
        el.sliderFill.style.width = (share * 100) + '%';
        el.sliderKnob.style.left = (share * 100) + '%';
        text(el.volumeValue, Math.round(volume * 100) + '%');
    }

    // A slider of divs: the range input is newer in Coherent than this panel
    // can count on. Dragged while the button is down, written on release.
    function wireSlider() {
        var dragging = false;
        var picked = null;
        function at(event) {
            var box = el.slider.getBoundingClientRect();
            var share = Math.max(0, Math.min(1, (event.clientX - box.left) / (box.width || 1)));
            // In steps of five per cent, as the window's slider moves.
            picked = Math.round(share * VOLUME_MAX * 20) / 20;
            paintVolume(picked);
        }
        el.slider.addEventListener('mousedown', function (event) {
            dragging = true;
            at(event);
        });
        document.addEventListener('mousemove', function (event) {
            if (dragging) at(event);
        });
        document.addEventListener('mouseup', function () {
            if (!dragging) return;
            dragging = false;
            if (picked !== null) save('audio.volume', picked);
        });
    }

    // ------------------------------------------------------------ landing ---

    /*
        The landing, as the application measured it. Nothing is worked out
        here -- the grade, the score and the schedule all arrive finished --
        for the same reason there is no air traffic control in this file.
    */
    var GRADES = { butter: ['tb.grade_butter', 'Butter'], smooth: ['tb.grade_smooth', 'Smooth'],
                   normal: ['tb.grade_normal', 'Normal'], firm: ['tb.grade_firm', 'Firm'],
                   hard: ['tb.grade_hard', 'Hard'] };
    var ON_TIME = { early: ['tb.early', 'early'], on_time: ['tb.on_time', 'on time'],
                    late: ['tb.late', 'late'] };

    function signed(minutes) {
        return (minutes > 0 ? '+' : minutes < 0 ? '−' : '±')
            + Math.abs(minutes) + ' min';
    }

    function paintLanding(report) {
        if (!report || !report.fresh || report.touchdown_at === landingAt) return;
        landingAt = report.touchdown_at;

        text(el.landingWhere, report.airport ? '· ' + report.airport : '');
        el.landingRate.innerHTML = '';
        el.landingRate.appendChild(document.createTextNode(
            report.fpm < 0 ? '−' + Math.abs(report.fpm) : String(report.fpm)));
        var unit = document.createElement('small');
        unit.textContent = 'fpm';
        el.landingRate.appendChild(unit);
        var grade = GRADES[report.grade];
        text(el.landingGrade, (grade ? t(grade[0], grade[1]) : report.grade)
            + '  ·  ' + report.score + '/100');

        var rows = [];
        rows.push(report.bounces
            ? t('tb.bounces', '{n} bounce(s)', { n: report.bounces })
            : t('tb.no_bounce', 'No bounce'));
        if (report.stable !== null && report.stable !== undefined) {
            rows.push(report.stable ? t('tb.stable', 'Stable approach')
                : t('tb.unsteady', 'Unsteady approach'));
        }
        if (report.arrival) {
            var when = ON_TIME[report.arrival];
            rows.push(t('tb.arrival', 'Arrival {delay}, {when}', {
                delay: signed(report.arrival_delay_min),
                when: when ? t(when[0], when[1]) : report.arrival }));
        }
        el.landingRows.innerHTML = '';
        for (var i = 0; i < rows.length; i++) {
            var div = document.createElement('div');
            div.textContent = rows[i];
            el.landingRows.appendChild(div);
        }

        el.landing.className = 'wa-landing is-shown';
        if (landingTimer) clearTimeout(landingTimer);
        landingTimer = setTimeout(hideLanding, LANDING_SHOWN_MS);
    }

    function hideLanding() {
        el.landing.className = 'wa-landing';
    }

    function veil(shown, detail) {
        cls(el.veil, 'wa-veil' + (shown ? ' is-shown' : ''));
        text(el.veilDetail, detail || '');
    }

    // ------------------------------------------------------------ the loop ---

    function paintState(state) {
        lastState = state;
        paintLink(state);
        paintRadio(state);
        paintStatus(state);
        paintReplies((state.flight || {}).phase || 'preflight');
        paintPtt(state);
        paintFlight(state);
    }

    function tick() {
        request('GET', '/api/state', null, function (state) {
            if (!state) return;
            // The settings carry the language: asked once the link is up,
            // again after it was lost, and every five seconds until answered.
            if (fails >= FAILS_BEFORE_VEIL
                    || (!settings && Date.now() - settingsAsked > 5000)) loadSettings();
            fails = 0;
            veil(false);
            paintState(state);
            paintLanding(state.landing);
        }, function (status) {
            fails += 1;
            if (fails >= FAILS_BEFORE_VEIL) {
                veil(true, status ? t('tb.answered', 'The application answered {status}', { status: status })
                    : t('tb.no_port', 'Nothing is listening on port {port}', { port: PORT }));
            }
        });
        if (tab === 'comms') {
            request('GET', '/api/transcript?limit=' + LINES, null, function (data) {
                if (data) paintFeed(data.transmissions);
            });
        }
    }

    function show(name) {
        tab = name;
        var tabs = el.tabs.querySelectorAll('.wa-tab');
        for (var i = 0; i < tabs.length; i++) {
            var on = tabs[i].getAttribute('data-tab') === name;
            tabs[i].className = 'wa-tab' + (on ? ' is-on' : '');
            cls($('wa-page-' + tabs[i].getAttribute('data-tab')), 'wa-page' + (on ? ' is-on' : ''));
        }
        if (name === 'set') loadSettings();
        if (name === 'comms') lastFeed = '';
        stationsAt = 0;
        tick();
    }

    // ----------------------------------------------------------- the keys ---

    function wire() {
        var ids = {
            link: 'wa-link', linkText: 'wa-link-text', tabs: 'wa-tabs',
            dot: 'wa-dot', freq: 'wa-freq', station: 'wa-station',
            standby: 'wa-standby', standbyName: 'wa-standby-name', swap: 'wa-swap',
            status: 'wa-status', feed: 'wa-feed', replies: 'wa-replies',
            compose: 'wa-compose', input: 'wa-input', send: 'wa-send',
            mic: 'wa-mic', key: 'wa-key',
            callsign: 'wa-callsign', type: 'wa-type', dep: 'wa-dep', dest: 'wa-dest',
            depName: 'wa-dep-name', destName: 'wa-dest-name', track: 'wa-track-fill',
            trackFill: 'wa-track-fill', trackDot: 'wa-track-dot', stations: 'wa-stations',
            rwy: 'wa-rwy', sid: 'wa-sid', squawk: 'wa-squawk', atis: 'wa-atis',
            atisLetter: 'wa-atis-letter', qnh: 'wa-qnh', wind: 'wa-wind',
            toggles: 'wa-toggles', slider: 'wa-slider', sliderFill: 'wa-slider-fill',
            sliderKnob: 'wa-slider-knob', volumeValue: 'wa-volume-value',
            veil: 'wa-veil', veilDetail: 'wa-veil-detail', landing: 'wa-landing',
            landingWhere: 'wa-landing-where', landingRate: 'wa-landing-rate',
            landingGrade: 'wa-landing-grade', landingRows: 'wa-landing-rows'
        };
        for (var name in ids) {
            if (ids.hasOwnProperty(name)) el[name] = $(ids[name]);
        }
        el.track = el.trackFill.parentNode;
        el.landing.addEventListener('click', hideLanding);

        var tabs = el.tabs.querySelectorAll('.wa-tab');
        for (var i = 0; i < tabs.length; i++) {
            (function (node) {
                node.addEventListener('click', function () { show(node.getAttribute('data-tab')); });
            })(tabs[i]);
        }

        // Push to talk is held, not toggled, which is what the key on the yoke
        // does and therefore what the finger expects. The pointer leaving the
        // key while it is down releases it too: a microphone that stays open
        // because the mouse slid off is the worst failure this panel has.
        function down(event) {
            if (event) event.preventDefault();
            holding = true;
            keyed = true;
            paintPtt(lastState);
            paintStatus(lastState);
            request('POST', '/api/ptt/start');
        }
        function up() {
            if (!holding) return;
            holding = false;
            request('POST', '/api/ptt/stop');
        }
        el.mic.addEventListener('mousedown', down);
        el.mic.addEventListener('mouseup', up);
        el.mic.addEventListener('mouseleave', up);

        // Typing. Keys pressed in a panel also reach the simulator's own
        // bindings unless the simulator is told an input field has them --
        // without this, typing "cleared to land" set the parking brake and
        // cycled the views. The same two calls every panel with a text box
        // makes (FlyByWire's EFB, among them): claim the keyboard on focus,
        // give it back on blur. Guarded, because outside the simulator there
        // is no Coherent to tell.
        var guid = 'wilcoatc-say-' + Date.now();
        function coherent(name) {
            try {
                if (typeof Coherent === 'undefined' || !Coherent.trigger) return;
                if (name === 'FOCUS_INPUT_FIELD') {
                    Coherent.trigger(name, guid, '', '', '', false);
                } else {
                    Coherent.trigger(name, guid);
                }
            } catch (e) { }
        }
        el.input.addEventListener('focus', function () {
            focused = true;
            coherent('FOCUS_INPUT_FIELD');
            paintPtt(lastState);
        });
        el.input.addEventListener('blur', function () {
            focused = false;
            coherent('UNFOCUS_INPUT_FIELD');
            paintPtt(lastState);
        });
        el.input.addEventListener('input', function () { paintPtt(lastState); });
        el.input.addEventListener('keydown', function (event) {
            var key = event.key || event.keyCode;
            if (key === 'Enter' || key === 13) {
                event.preventDefault();
                sendTyped();
            } else if (key === 'Escape' || key === 27) {
                // Give the keyboard back to the aeroplane.
                el.input.blur();
            }
        });
        el.send.addEventListener('mousedown', function (event) {
            // Before the input loses focus, so the text is still there.
            event.preventDefault();
            sendTyped();
        });

        // Swap in the aeroplane as well as here: the same event the button on
        // the radio stack sends, so the cockpit and the panel never disagree.
        el.swap.addEventListener('click', function () {
            if (!standby) return;
            request('POST', '/api/swap', { radio: 1 }, tick);
        });
        el.atis.addEventListener('click', function () {
            request('POST', '/api/atis', {});
        });
        wireSlider();
    }

    /*
        What was typed, as a transmission. The application puts it in the
        transcript as the pilot's and hands it to the same parser a spoken one
        goes to, so it is answered the same way. The box keeps focus, so the
        next line can be typed straight after -- a readback, usually.
    */
    function sendTyped() {
        var said = (el.input.value || '').replace(/^\s+|\s+$/g, '');
        if (!said) return;
        el.input.value = '';
        sending = true;
        paintPtt(lastState);
        paintStatus(lastState);
        request('POST', '/api/say', { text: said }, function () {
            setTimeout(function () { sending = false; paintStatus(lastState); }, 1200);
            tick();
        }, function () {
            // Not sent: put it back rather than losing what was typed.
            sending = false;
            if (!el.input.value) el.input.value = said;
            paintPtt(lastState);
            paintStatus(lastState);
        });
    }

    function start() {
        wire();
        paintWords();
        tick();
        if (timer) clearInterval(timer);
        timer = setInterval(tick, PERIOD);
    }

    // The panel's markup is templated by the simulator, so the elements are
    // not there when this file is parsed. Waiting for the document rather than
    // running immediately is what stops every getElementById returning null.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
