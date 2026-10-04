/*
 * The toolbar panel, inside the simulator.
 *
 * It contains no air traffic control at all. The phraseology, the voices, the
 * speech recognition and the transcript all live in the application; this
 * polls two endpoints, redraws, and sends a handful of commands back. That is
 * deliberate: any rule duplicated here is a rule that will one day disagree
 * with the one the radio is actually using. The one table it does carry --
 * the quick replies -- is the desktop window's, word for word, and a test
 * holds the two together.
 *
 * XMLHttpRequest rather than fetch. The panel runs in Coherent GT, whose
 * embedded version moves between simulator builds; XHR has always been there
 * and fetch has not. The gain from fetch would be cosmetic and the failure
 * would be a blank panel with no message in it. For the same reason this is
 * ES5: no arrow functions, no let, no template strings.
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

    // How many transmissions the conversation shows. Enough to see the
    // instruction you are reading back and what came before it; the whole
    // transcript is the application's window.
    var LINES = 4;

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

    var el = {};
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
    // Which touchdown the landing card was last shown for, so each landing
    // comes up once however many times the state is polled.
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
            xhr.timeout = 2000;
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

    function paintRadio(state) {
        var radio = state.radio || {};
        var tuned = radio.com1 || 0;
        text(el.freq, mhz(tuned));
        cls(el.freq, 'wa-freq' + (tuned ? '' : ' is-empty'));

        // Who answers, and how far away. The station name in full strength,
        // the distance after it quieter, because it qualifies the name.
        var station = radio.station || '';
        var line = station || (tuned ? 'Nobody on this frequency'
            : (state.connected ? 'Nothing tuned' : 'No aircraft'));
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
        text(el.standby, mhz(standby));
        cls(el.swap, 'wa-swap' + (standby ? '' : ' is-off'));
    }

    function paintField(state) {
        var wx = state.weather;
        text(el.atis, state.atis_letter || '–');
        if (wx) {
            text(el.qnh, String(wx.qnh_hpa));
            // The way the METAR and the ATIS card write it: 050° 12G22.
            var wind = wx.wind_kt < 1 ? 'Calm'
                : three(wx.wind_dir) + '° ' + wx.wind_kt
                  + (wx.gust_kt > wx.wind_kt ? 'G' + wx.gust_kt : '')
                  + (wx.gust_kt > wx.wind_kt ? '' : ' kt');
            text(el.wind, wind);
        } else {
            text(el.qnh, '–');
            text(el.wind, '–');
        }
        var flight = state.flight || {};
        var position = state.position || {};
        text(el.squawk, flight.squawk || position.squawk || '–');
    }

    function paintClearance(state) {
        var flight = state.flight || {};
        var phase = flight.phase || 'preflight';
        var runway = flight.cleared_runway || flight.runway || '';
        var waiting = !!flight.pending;

        text(el.phase, waiting ? 'Read back' : (PHASE_NAMES[phase] || phase));

        // The last thing it was cleared to do, in the words on the strip.
        var cleared = '';
        if (flight.cleared_landing) cleared = 'Cleared to land' + (runway ? ' ' + runway : '');
        else if (flight.cleared_takeoff) cleared = 'Cleared for takeoff' + (runway ? ' ' + runway : '');
        else if (waiting) cleared = 'Awaiting your readback';
        else if (runway) cleared = 'Runway ' + runway;
        else if (flight.destination) {
            cleared = (flight.departure ? flight.departure + ' → ' : '→ ')
                + flight.destination;
        }
        text(el.cleared, cleared);

        var alt = flight.assigned_altitude_ft;
        text(el.assigned, alt ? (alt >= 18000
            ? 'FL' + Math.round(alt / 100) : thousands(alt) + ' ft') : '');

        cls(el.clear, 'wa-clear' + (waiting ? ' is-waiting' : ''));
        paintReplies(phase);
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
        node.textContent = phrase;
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
        var playing = !!state.playing;
        var key = state.ptt_key ? String(state.ptt_key).toUpperCase() : '';
        var typed = !!el.input.value;
        var status = '';
        if (keyed) status = 'Transmitting — release to send';
        else if (sending) status = 'Sent';
        else if (playing) status = 'Controller speaking';

        cls(el.mic, 'wa-mic' + (keyed ? ' is-live'
            : playing ? ' is-receiving' : typed ? ' is-quiet' : ''));
        cls(el.compose, 'wa-compose' + (keyed ? ' is-live'
            : focused ? ' is-focused' : ''));
        cls(el.send, 'wa-send' + (typed && !keyed ? ' is-shown' : ''));
        el.mic.title = key ? 'Hold to talk (or hold ' + key + ')' : 'Hold to talk';

        text(el.status, status);
        cls(el.status, 'wa-status' + (status ? ' is-shown' : '')
            + (keyed ? ' is-live' : ''));
    }

    function paintState(state) {
        lastState = state;
        paintRadio(state);
        paintField(state);
        paintClearance(state);
        paintPtt(state);
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
        if (kind === 'pilot') return 'You';
        if (line.kind === 'atis') return 'ATIS';
        if (line.kind === 'crew') return line.speaker || 'Crew';
        return line.speaker || (kind === 'atc' ? 'ATC' : 'Traffic');
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
            empty.textContent = 'Nothing heard yet';
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

    /*
        The landing, as the application measured it. Nothing is worked out
        here -- the grade, the score and the schedule all arrive finished --
        for the same reason there is no air traffic control in this file.
    */
    var GRADES = { butter: 'Butter', smooth: 'Smooth', normal: 'Normal',
                   firm: 'Firm', hard: 'Hard' };
    var ON_TIME = { early: 'early', on_time: 'on time', late: 'late' };

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
        text(el.landingGrade, (GRADES[report.grade] || report.grade)
            + '  ·  ' + report.score + '/100');

        var rows = [];
        rows.push(report.bounces
            ? report.bounces + (report.bounces > 1 ? ' bounces' : ' bounce')
            : 'No bounce');
        if (report.stable !== null && report.stable !== undefined) {
            rows.push((report.stable ? 'Stable' : 'Unsteady') + ' approach');
        }
        if (report.arrival) {
            rows.push('Arrival ' + signed(report.arrival_delay_min)
                + ', ' + ON_TIME[report.arrival]);
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

    function tick() {
        request('GET', '/api/state', null, function (state) {
            if (!state) return;
            fails = 0;
            veil(false);
            paintState(state);
            paintLanding(state.landing);
        }, function (status) {
            fails += 1;
            if (fails >= FAILS_BEFORE_VEIL) {
                veil(true, status ? 'The application answered ' + status
                    : 'Nothing is listening on port ' + PORT);
            }
        });
        request('GET', '/api/transcript?limit=' + LINES, null, function (data) {
            if (data) paintFeed(data.transmissions);
        });
    }

    // ----------------------------------------------------------- the keys ---

    function wire() {
        el.dot = $('wa-dot');
        el.freq = $('wa-freq');
        el.station = $('wa-station');
        el.standby = $('wa-standby');
        el.swap = $('wa-swap');
        el.atis = $('wa-atis-letter');
        el.qnh = $('wa-qnh');
        el.wind = $('wa-wind');
        el.squawk = $('wa-squawk');
        el.clear = $('wa-clear');
        el.phase = $('wa-phase');
        el.cleared = $('wa-cleared');
        el.assigned = $('wa-assigned');
        el.feed = $('wa-feed');
        el.replies = $('wa-replies');
        el.status = $('wa-status');
        el.compose = $('wa-compose');
        el.input = $('wa-input');
        el.send = $('wa-send');
        el.mic = $('wa-mic');
        el.veil = $('wa-veil');
        el.veilDetail = $('wa-veil-detail');
        el.landing = $('wa-landing');
        el.landingWhere = $('wa-landing-where');
        el.landingRate = $('wa-landing-rate');
        el.landingGrade = $('wa-landing-grade');
        el.landingRows = $('wa-landing-rows');
        el.landing.addEventListener('click', hideLanding);

        // Push to talk is held, not toggled, which is what the key on the yoke
        // does and therefore what the finger expects. The pointer leaving the
        // key while it is down releases it too: a microphone that stays open
        // because the mouse slid off is the worst failure this panel has.
        function down(event) {
            if (event) event.preventDefault();
            holding = true;
            keyed = true;
            paintPtt(lastState);
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
        $('wa-atis').addEventListener('click', function () {
            request('POST', '/api/atis', {});
        });
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
        request('POST', '/api/say', { text: said }, function () {
            setTimeout(function () { sending = false; paintPtt(lastState); }, 1200);
            tick();
        }, function () {
            // Not sent: put it back rather than losing what was typed.
            sending = false;
            if (!el.input.value) el.input.value = said;
            paintPtt(lastState);
        });
    }

    function start() {
        wire();
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
