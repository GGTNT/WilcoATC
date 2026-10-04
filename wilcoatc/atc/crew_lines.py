# -*- coding: utf-8 -*-
"""What the crew actually say.

Kept apart from :mod:`wilcoatc.atc.crew`, which decides *when* somebody
speaks. This file is only the words, and it is the one place in the program
where the words are not phraseology: a purser telling you the local time is
neither correct nor incorrect, and it should not live next to the sentences
that are.

The lines are written out rather than generated. A cabin announcement is a
small piece of writing, and the alternatives are what stop it becoming
wallpaper by the third flight -- but they are alternatives to a fixed thing,
not a grammar, because a grammar produces sentences nobody would say.

A line that says something about the aeroplane -- the beacon, the seatbelt
sign, the gear -- carries the name of a condition in :data:`CONDITIONS`, and
:mod:`wilcoatc.atc.crew` checks it when the line comes due. That is why a claim
about a switch is its own line rather than a clause in the middle of another
one: "brakes released, cleared to push" is true whatever the beacon is doing,
and "beacon is on" is not.

Substitutions available to every line:

  {airline}      what the crew call the airline, or "this flight"
  {service}      the flight as it is announced -- "Delta flight twelve
                 thirty four" for an airline, "this flight" for a private
                 registration, which is why the two are one substitution:
                 "{airline} flight {flight}" came out as "this flight
                 flight November one seven two Sierra Papa"
  {origin}       departure city or airport
  {destination}  arrival city or airport
  {captain}      the captain's surname
  {cruise}       cruising level, spoken
  {time}         en-route time, spoken
  {local}        local time at the destination
  {temperature}  temperature at the destination
  {runway}       the runway in use
"""

from __future__ import annotations

from dataclasses import dataclass

# The four voices. FO and CAPTAIN in the flight deck are on the interphone;
# the same captain making a cabin announcement is on the address system --
# which is why the path belongs to the line rather than to the person.
FIRST_OFFICER = "FO"
CAPTAIN = "CAPTAIN"
PURSER = "PURSER"
CABIN = "CABIN"
# The man on the headset under the nose. Not crew and not a controller: he
# answers the flight deck on the interphone, he only ever says anything
# because he was asked, and what he says is the ground handling actually
# doing it. He is here rather than in the controller because nothing he says
# goes out on the air.
GROUND = "GROUND"

# Where a line comes out.
INTERCOM = "intercom"        # the flight deck, headset to headset
CABIN_PA = "cabin"           # the address system, heard from the flight deck

# The gap between two paragraphs of one continuous announcement, as opposed
# to the gap between two people speaking.
#
# The safety briefing is the only thing in this file that is one person
# talking without stopping, and it was written with three seconds between
# its paragraphs -- the same pause as between a purser calling the cabin
# secure and the first officer answering. Three seconds is not a breath, it
# is a hole, and the briefing arrived out of it in chunks: two sentences,
# silence, two more. A purser reading a demonstration takes a breath and
# carries on.
BRIEFING_BREATH = 0.8


@dataclass(frozen=True)
class Line:
    """One thing somebody says."""

    who: str
    text: str
    path: str = INTERCOM
    # A chime before it, and which way the two notes go. "" for none.
    chime: str = ""
    # Held back this long after the line before it, so a sequence plays as a
    # sequence rather than as one wall of speech.
    after_s: float = 0.0
    # What has to be true of the aeroplane for the line to still be worth
    # saying, by name from CONDITIONS. Checked when the line comes due rather
    # than when it was queued, because that is the whole point: a cue fires on
    # an event and the line is spoken seconds later, by which time "rotate"
    # can be a call to an aeroplane already five hundred feet up.
    needs: str = ""
    # How long to hold a line whose condition is not true yet. Zero drops it
    # at once, which is what a callout wants -- a late "eighty knots" is not
    # worth having. A cabin announcement about a switch is the other case:
    # the purser says the seatbelt sign is off when the pilot actually turns
    # it off, which may be a minute after ten thousand feet.
    patience_s: float = 0.0
    # The language it is said in, when that is not the crew's own: the
    # English repeat of a French announcement. Empty is the crew's language.
    language: str = ""


# --------------------------------------------------------------------------
# what has to be true for a line to be worth saying
# --------------------------------------------------------------------------
#
# Each takes the aircraft state and the set of switches this aeroplane has
# been seen to work, and answers whether the line is still true. They are
# named from the line rather than called from it so that the words stay a
# table of words: nothing in this file imports anything.
#
# The "seen" set is what keeps an aeroplane that does not model a switch from
# going silent. A light single has no seatbelt sign, reports it off for the
# whole flight, and would never hear the cabin announcements if the sign were
# simply believed. So the sign is only believed once it has been seen on --
# which is to say, once the aeroplane has shown it has one.


def _rolling(state, seen) -> bool:
    """Still on the ground. A takeoff callout after liftoff is noise."""
    return bool(getattr(state, "on_ground", False))


def _beacon_on(state, seen) -> bool:
    """The beacon is actually on. Every aeroplane here has one."""
    return bool(getattr(state, "beacon_light", False))


def _gear_down(state, seen) -> bool:
    """There is still gear to raise, so "gear up" is an instruction."""
    return bool(getattr(state, "gear_down", False))


def _gear_up(state, seen) -> bool:
    """The gear is up, so a checklist may report it up."""
    return not getattr(state, "gear_down", False)


def _signs_off(state, seen) -> bool:
    """The seatbelt sign is off, or this aeroplane has not got one."""
    return "seatbelt" not in seen or not getattr(state, "seatbelt_sign", False)


def _signs_on(state, seen) -> bool:
    """The seatbelt sign is on, or this aeroplane has not got one."""
    return "seatbelt" not in seen or bool(getattr(state, "seatbelt_sign", False))


CONDITIONS = {
    "rolling": _rolling,
    "beacon_on": _beacon_on,
    "gear_down": _gear_down,
    "gear_up": _gear_up,
    "signs_off": _signs_off,
    "signs_on": _signs_on,
}

# How long the cabin will wait for a switch before giving up on the line. Long
# enough for a pilot who is flying the aeroplane rather than watching for a
# cue, and short enough that the announcement still belongs to the part of the
# flight it was written for.
SWITCH_PATIENCE_S = 300.0


EN: dict[str, tuple[tuple[Line, ...], ...]] = {
    # ------------------------------------------------------------- gate ---
    "boarding": (
        (
            Line(PURSER, "Good day ladies and gentlemen, and welcome aboard "
                         "{service}, with service to "
                         "{destination}. Please stow your cabin baggage in "
                         "the overhead lockers or completely under the seat "
                         "in front of you, and take your seats as quickly as "
                         "you can.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(PURSER, "Ladies and gentlemen, welcome aboard "
                         "{service}, bound for {destination}. "
                         "Boarding is nearly complete, so please make your "
                         "way to your seat and stow your bags either "
                         "overhead or under the seat in front of you.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "doors_closed": (
        (
            Line(PURSER, "Cabin crew, doors to automatic and cross check.",
                 CABIN_PA),
            Line(CABIN, "Doors are armed and cross checked.",
                 CABIN_PA, after_s=5.0),
        ),
        (
            Line(PURSER, "Cabin crew, arm doors and cross check.", CABIN_PA),
            Line(CABIN, "Armed and cross checked, thank you.",
                 CABIN_PA, after_s=5.0),
        ),
    ),
    "pushback": (
        (
            Line(FIRST_OFFICER, "Brakes released, cleared to push.", INTERCOM),
            # The beacon is a switch, not a formality: an aeroplane pushed
            # back with it off is one the ramp cannot see is about to move.
            # So the first officer says what is true -- it is on, or it is
            # not -- and the two lines are gated against each other.
            Line(FIRST_OFFICER, "Beacon is on.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
        (
            Line(FIRST_OFFICER, "Parking brake off, push approved.", INTERCOM),
            Line(FIRST_OFFICER, "Beacon on, we are clear to push.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
    ),
    # The captain's welcome, made while the aeroplane is being pushed.
    #
    # There is a second one in the cruise, and they are different
    # announcements rather than the same one twice. This one is made before
    # anybody has gone anywhere: it says who is flying, where to, and roughly
    # how long, and it deliberately says nothing about the cruise, because
    # the aeroplane is on the stand and the level is not a fact yet. The
    # cruise one is the opposite -- it exists to say "we have levelled off at
    # this height" -- which is why neither reads as a repeat of the other.
    "captain_welcome": (
        (
            Line(CAPTAIN, "Good day ladies and gentlemen, this is Captain "
                          "{captain} speaking from the flight deck. Welcome "
                          "aboard {service} to {destination}. We are being "
                          "pushed back from the stand now, and our flight "
                          "time today should be about {time}. Please make "
                          "sure your seatbelt is fastened, and sit back "
                          "and enjoy the flight.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Ladies and gentlemen, this is Captain {captain} "
                          "from the flight deck. Welcome aboard {service}, "
                          "and thank you for joining us here at {origin}. We "
                          "are pushing back for {destination}, where we "
                          "expect to be in about {time}. Please take your "
                          "seats and keep your seatbelt fastened while we "
                          "are on the move.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Ladies and gentlemen, good day from the flight "
                          "deck. Captain {captain} and the crew would like "
                          "to welcome you aboard {service}, bound for "
                          "{destination}. The push has been approved and we "
                          "will be starting engines shortly. Our time in the "
                          "air today is around {time}. Thank you for flying "
                          "with us.",
                 CABIN_PA, chime="up"),
        ),
    ),
    # The purser, once the aeroplane is off the stand and the push is over.
    #
    # This is the announcement a real cabin crew make and this program used
    # not to: the captain introduced himself at the push and then nobody in
    # the back said who they were for the rest of the flight. It is the one
    # place anybody is named -- the cabin manager gives their own first name
    # and the names of the crew working with them -- which is why it is late
    # enough to have the engines running behind it and early enough to be
    # over before the demonstration starts.
    "cabin_welcome": (
        (
            Line(PURSER, "Ladies and gentlemen, on behalf of Captain "
                         "{captain} and the whole crew, welcome aboard "
                         "{service} to {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "My name is {purser}, I am your cabin manager on "
                         "this flight, and looking after you today I have "
                         "{crew}. If there is anything at all you need, "
                         "please do ask any one of us.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "A little after departure we will be coming "
                         "through the cabin with drinks, followed by a meal "
                         "service.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "On behalf of the crew, thank you for choosing "
                         "{airline}. We hope you enjoy the flight.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Good day ladies and gentlemen, and welcome aboard "
                         "{service}, bound for {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "My name is {purser} and I am the cabin manager on "
                         "board today. Working with me in the cabin are "
                         "{crew}, and we are here to look after you for the "
                         "whole of the flight.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "A drinks service will follow shortly after "
                         "departure, and a meal will be served during the "
                         "flight.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Thank you for choosing {airline}, and we hope you "
                         "have a pleasant flight with us.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "safety_briefing": (
        (
            Line(PURSER, "Ladies and gentlemen, we ask for your attention "
                         "for a few moments while we point out the safety "
                         "features of this aircraft.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Your seatbelt fastens like this, and is released "
                         "like this. Please keep it fastened whenever you "
                         "are seated, low and tight across your hips.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "There are exits on both sides of the cabin, and "
                         "the nearest one may be behind you. The path to the "
                         "exits is marked at floor level and will light up "
                         "if the cabin lighting fails.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Should the cabin lose pressure, a mask will drop "
                         "in front of you. Pull it towards you, place it "
                         "over your nose and mouth, and breathe normally. "
                         "Fit your own before helping anybody else.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Your lifejacket is under your seat. Slip it over "
                         "your head, fasten the straps at the front, and "
                         "inflate it only once you are outside the aircraft.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Smoking is not permitted anywhere on board, "
                         "including the lavatories, which are fitted with "
                         "smoke detectors. The safety card in the seat "
                         "pocket has all of this on it, and we ask you to "
                         "read it. Thank you.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Ladies and gentlemen, may we have your attention "
                         "for the safety briefing.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Please fasten your seatbelt now, low and tight "
                         "across your hips, and keep it fastened whenever "
                         "the sign is lit.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Take a moment to locate your nearest exit, bearing "
                         "in mind that it may be behind you. Floor level "
                         "lighting will guide you to it.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "In the event of a loss of cabin pressure, oxygen "
                         "masks will appear above you. Pull one firmly "
                         "towards you, cover your nose and mouth, and "
                         "breathe normally.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Your lifejacket is beneath your seat, and is "
                         "inflated only after leaving the aircraft. "
                         "Everything we have shown you is on the safety card "
                         "in front of you. Thank you for your attention.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    # ------------------------------------------------------------- taxi ---
    "taxi": (
        (
            Line(FIRST_OFFICER, "Taxi checklist complete. Flaps set, flight "
                                "controls checked.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Taxi checks are done, flaps set for "
                                "departure.", INTERCOM),
        ),
    ),
    "cabin_secure": (
        (
            Line(PURSER, "Cabin crew, take your seats for departure.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabin is secure.", INTERCOM, after_s=6.0),
        ),
        (
            Line(PURSER, "Cabin crew, please be seated for takeoff.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabin secure, we are ready.",
                 INTERCOM, after_s=6.0),
        ),
    ),
    "before_takeoff": (
        (
            Line(FIRST_OFFICER, "Line up checklist complete. Runway "
                                "{runway}, cleared for takeoff.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Checklist complete, runway {runway}. Your "
                                "controls.", INTERCOM),
        ),
    ),
    # ---------------------------------------------------------- takeoff ---
    "eighty": ((Line(FIRST_OFFICER, "Eighty knots.", INTERCOM,
                     needs="rolling"),),),
    "v_one": ((Line(FIRST_OFFICER, "V one. Rotate.", INTERCOM,
                    needs="rolling"),),),
    "positive_rate": (
        (
            Line(FIRST_OFFICER, "Positive rate.", INTERCOM),
            Line(FIRST_OFFICER, "Gear up.", INTERCOM, after_s=2.0,
                 needs="gear_down"),
        ),
    ),
    "after_takeoff": (
        (
            Line(FIRST_OFFICER, "After takeoff checklist complete.",
                 INTERCOM),
            # A checklist that reports the gear up says so only when it is.
            # The rest of the call stands either way, so a pilot who has left
            # it down loses the claim rather than the checklist.
            Line(FIRST_OFFICER, "Gear up, flaps coming up on schedule.",
                 INTERCOM, after_s=1.5, needs="gear_up", patience_s=60.0),
        ),
        (
            Line(FIRST_OFFICER, "After takeoff checks complete.", INTERCOM),
        ),
    ),
    "ten_thousand_up": (
        (
            Line(FIRST_OFFICER, "Ten thousand feet.", INTERCOM),
            # The cabin is told about a switch, so it waits for the switch.
            # Ten thousand feet is when the sign usually goes off and not
            # when it must, and a purser announcing a sign that is still lit
            # is the crew reading a script over the pilot's head.
            Line(PURSER, "Ladies and gentlemen, the captain has switched off "
                         "the seatbelt sign. You are free to move about the "
                         "cabin, though we do ask you to keep your belt "
                         "loosely fastened while you are seated.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
        (
            Line(FIRST_OFFICER, "Ten thousand.", INTERCOM),
            Line(PURSER, "Ladies and gentlemen, the seatbelt sign is now "
                         "off. We would still ask you to keep your belt "
                         "fastened whenever you are in your seat, in case of "
                         "unexpected turbulence.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    # ----------------------------------------------------------- cruise ---
    "welcome": (
        (
            Line(CAPTAIN, "Good day ladies and gentlemen, this is Captain "
                          "{captain} from the flight deck. Welcome aboard "
                          "{service}, on our way to {destination}. We "
                          "have levelled off at {cruise}, our flight time "
                          "today is {time}, and we are expecting a smooth "
                          "ride. Sit back, relax, and enjoy the flight.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Ladies and gentlemen, this is your captain "
                          "speaking. We are now cruising at {cruise} on our "
                          "way to {destination}, and we expect to be with "
                          "you for {time}. The cabin crew will be coming "
                          "through the cabin shortly. Thank you for flying "
                          "with us.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "service": (
        (
            Line(PURSER, "Ladies and gentlemen, the cabin crew will shortly "
                         "be passing through the cabin with a selection of "
                         "drinks and light refreshments.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Good afternoon. Would you care for anything to "
                        "drink? We have tea, coffee, juice and soft drinks.",
                 CABIN_PA, after_s=20.0),
        ),
        (
            Line(PURSER, "Ladies and gentlemen, our cabin service is about "
                         "to begin. We will be through the cabin with drinks "
                         "and a light meal.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Something to drink for you? We have water, juice, "
                        "tea and coffee.",
                 CABIN_PA, after_s=20.0),
        ),
    ),
    "service_cockpit": (
        (
            Line(PURSER, "Flight deck, would either of you like anything? We "
                         "have coffee on.", INTERCOM),
        ),
        (
            Line(PURSER, "Anything from the galley for the flight deck?",
                 INTERCOM),
        ),
    ),
    "cruise_chat": (
        (
            Line(FIRST_OFFICER, "Fuel is on plan. Nothing on the weather "
                                "radar ahead.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "We are a couple of minutes up on the flight "
                                "plan, and the ride looks smooth all the "
                                "way.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Systems are all normal, fuel checks good.",
                 INTERCOM),
        ),
    ),
    # ---------------------------------------------------------- descent ---
    "top_of_descent": (
        (
            Line(CAPTAIN, "Ladies and gentlemen, we have begun our descent "
                          "into {destination}. The local time there is "
                          "{local} and the temperature is {temperature}. "
                          "Cabin crew, please prepare the cabin for landing.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Ladies and gentlemen, we have started our descent "
                          "towards {destination}, where the temperature is "
                          "{temperature} and the local time is {local}. "
                          "Cabin crew, prepare the cabin for arrival, "
                          "please.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "ten_thousand_down": (
        (
            Line(FIRST_OFFICER, "Ten thousand feet. Lights on.", INTERCOM),
            Line(PURSER, "Ladies and gentlemen, the seatbelt sign is now on. "
                         "Please return to your seat, fasten your belt, and "
                         "make sure your tray table is stowed and your seat "
                         "back is upright.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_on", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "cabin_ready": (
        (
            Line(PURSER, "Cabin crew, take your seats for landing.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabin is secure for landing.",
                 INTERCOM, after_s=6.0),
        ),
        (
            Line(PURSER, "Cabin secure. Crew, please be seated.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabin reports secure.",
                 INTERCOM, after_s=6.0),
        ),
    ),
    "approach": (
        (
            Line(FIRST_OFFICER, "Landing checklist complete. Gear down, "
                                "three greens, flaps set.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Landing checks complete. Gear down and "
                                "locked.", INTERCOM),
        ),
    ),
    "one_thousand": (
        (Line(FIRST_OFFICER, "One thousand. Stabilised.", INTERCOM),),
    ),
    "five_hundred": ((Line(FIRST_OFFICER, "Five hundred.", INTERCOM),),),
    "minimums": (
        (Line(FIRST_OFFICER, "Minimums. Runway in sight.", INTERCOM),),
    ),
    # ---------------------------------------------------------- landing ---
    "touchdown": (
        (
            Line(FIRST_OFFICER, "Spoilers up. Reverse green.", INTERCOM),
            Line(FIRST_OFFICER, "Eighty knots.", INTERCOM, after_s=4.0),
            Line(FIRST_OFFICER, "Sixty knots. Reversers to idle.",
                 INTERCOM, after_s=3.0),
        ),
    ),
    "landed": (
        (
            Line(PURSER, "Ladies and gentlemen, welcome to {destination}. "
                         "The local time is {local} and the temperature is "
                         "{temperature}. Please remain seated with your "
                         "seatbelt fastened until the aircraft has come to a "
                         "complete stop and the seatbelt sign has been "
                         "switched off.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(PURSER, "Ladies and gentlemen, welcome to {destination}, "
                         "where the local time is {local}. Please stay "
                         "seated with your belt fastened until we are on "
                         "stand and the sign goes off.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "taxi_in": (
        (
            Line(FIRST_OFFICER, "After landing checklist complete. Flaps up, "
                                "spoilers stowed.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "After landing checks complete.", INTERCOM),
        ),
    ),
    "parked": (
        (
            Line(PURSER, "Cabin crew, doors to manual and cross check.",
                 CABIN_PA, chime="down"),
            Line(PURSER, "Ladies and gentlemen, on behalf of {airline} and "
                         "the entire crew, thank you for flying with us "
                         "today. Please take all your belongings with you, "
                         "and we hope to see you again soon.",
                 CABIN_PA, after_s=7.0),
        ),
        (
            Line(PURSER, "Cabin crew, disarm doors and cross check.",
                 CABIN_PA, chime="down"),
            Line(PURSER, "Ladies and gentlemen, thank you for choosing "
                         "{airline}. Please check around you for your "
                         "personal belongings before you leave, and we wish "
                         "you a pleasant onward journey.",
                 CABIN_PA, after_s=7.0),
        ),
    ),
    # ----------------------------------------------------------- others ---
    "turbulence": (
        (
            Line(PURSER, "Ladies and gentlemen, the captain has switched the "
                         "seatbelt sign back on. Please return to your seat "
                         "and fasten your belt.",
                 CABIN_PA, chime="down"),
        ),
    ),
}


FR: dict[str, tuple[tuple[Line, ...], ...]] = {
    "boarding": (
        (
            Line(PURSER, "Mesdames et messieurs, bienvenue à bord de "
                         "{service} à destination de {destination}. "
                         "Nous vous demandons de ranger vos bagages dans les "
                         "coffres ou sous le siège devant vous, et de "
                         "rejoindre votre place.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "doors_closed": (
        (
            Line(PURSER, "Personnel de cabine, armement des toboggans et "
                         "vérification croisée.", CABIN_PA),
            Line(CABIN, "Toboggans armés et vérifiés.",
                 CABIN_PA, after_s=5.0),
        ),
    ),
    "pushback": (
        (
            Line(FIRST_OFFICER, "Frein de parc desserré, repoussage "
                                "autorisé.", INTERCOM),
            Line(FIRST_OFFICER, "Balise allumée.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
    ),
    "captain_welcome": (
        (
            Line(CAPTAIN, "Mesdames et messieurs, ici le commandant de bord "
                          "{captain}. Bienvenue à bord de {service} à "
                          "destination de {destination}. Nous quittons le "
                          "poste de stationnement, et notre temps de vol sera "
                          "d'environ {time}. Veuillez regagner votre siège et "
                          "attacher votre ceinture. Bon vol à tous.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Mesdames et messieurs, le commandant {captain} et "
                          "tout l'équipage vous souhaitent la bienvenue à "
                          "bord de {service} au départ de {origin}. Le "
                          "repoussage est en cours et nous démarrerons les "
                          "moteurs dans quelques instants. Nous devrions "
                          "arriver à {destination} dans environ {time}.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Mesdames et messieurs, bonjour, ici votre "
                          "commandant de bord {captain}. Nous sommes "
                          "actuellement repoussés pour {destination}, et le "
                          "vol durera environ {time}. Merci d'avoir choisi de "
                          "voyager avec nous, et restez assis, ceinture "
                          "attachée, pendant la manœuvre.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "cabin_welcome": (
        (
            Line(PURSER, "Mesdames et messieurs, au nom du commandant de "
                         "bord {captain} et de tout l'équipage, "
                         "bienvenue à bord de {service} à "
                         "destination de {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Je m'appelle {purser}, je suis votre chef de "
                         "cabine sur ce vol, et en cabine avec moi "
                         "aujourd'hui, {crew}. N'hésitez pas "
                         "à nous solliciter si vous avez besoin de quoi "
                         "que ce soit.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Peu après le décollage, nous passerons en "
                         "cabine avec des boissons, puis un repas vous sera "
                         "servi.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Au nom de tout l'équipage, merci "
                         "d'avoir choisi {airline}. Nous vous souhaitons "
                         "un excellent vol.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Mesdames et messieurs, bienvenue à bord de "
                         "{service}, à destination de {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Je suis {purser}, chef de cabine, et "
                         "l'équipage qui vous accompagne "
                         "aujourd'hui est composé de {crew}.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Un service de boissons sera proposé après "
                         "le décollage, suivi d'un repas au cours "
                         "du vol.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Merci d'avoir choisi {airline}, et très "
                         "bon vol à toutes et à tous.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "safety_briefing": (
        (
            Line(PURSER, "Mesdames et messieurs, nous vous demandons "
                         "quelques instants d'attention pour la présentation "
                         "des consignes de sécurité.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Votre ceinture s'attache ainsi et se détache "
                         "ainsi. Gardez-la attachée dès que vous êtes assis, "
                         "bien serrée sur les hanches.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Cet appareil dispose de sorties de secours de "
                         "chaque côté de la cabine. La plus proche peut se "
                         "trouver derrière vous, et le chemin qui y mène est "
                         "balisé au sol.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "En cas de dépressurisation, un masque tombera "
                         "devant vous. Tirez-le vers vous, appliquez-le sur "
                         "le nez et la bouche, et respirez normalement.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Votre gilet de sauvetage se trouve sous votre "
                         "siège. Il ne doit être gonflé qu'une fois à "
                         "l'extérieur de l'appareil. Il est interdit de "
                         "fumer à bord. Merci de votre attention.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "taxi": (
        (
            Line(FIRST_OFFICER, "Check roulage terminée. Volets sortis, "
                                "commandes de vol vérifiées.", INTERCOM),
        ),
    ),
    "cabin_secure": (
        (
            Line(PURSER, "Personnel de cabine, veuillez vous asseoir pour le "
                         "décollage.", CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabine parée.", INTERCOM, after_s=6.0),
        ),
    ),
    "before_takeoff": (
        (
            Line(FIRST_OFFICER, "Check avant décollage terminée. Piste "
                                "{runway}, autorisés au décollage.",
                 INTERCOM),
        ),
    ),
    "eighty": ((Line(FIRST_OFFICER, "Quatre-vingts nœuds.", INTERCOM,
                     needs="rolling"),),),
    "v_one": ((Line(FIRST_OFFICER, "V un. Rotation.", INTERCOM,
                    needs="rolling"),),),
    "positive_rate": (
        (
            Line(FIRST_OFFICER, "Vario positif.", INTERCOM),
            Line(FIRST_OFFICER, "Train rentré.", INTERCOM, after_s=2.0,
                 needs="gear_down"),
        ),
    ),
    "after_takeoff": (
        (
            Line(FIRST_OFFICER, "Check après décollage terminée.", INTERCOM),
            Line(FIRST_OFFICER, "Train rentré, volets en rentrée.", INTERCOM,
                 after_s=1.5, needs="gear_up", patience_s=60.0),
        ),
    ),
    "ten_thousand_up": (
        (
            Line(FIRST_OFFICER, "Dix mille pieds.", INTERCOM),
            Line(PURSER, "Mesdames et messieurs, le commandant de bord a "
                         "éteint le signal lumineux. Nous vous conseillons "
                         "néanmoins de garder votre ceinture attachée "
                         "lorsque vous êtes assis.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "welcome": (
        (
            Line(CAPTAIN, "Mesdames et messieurs, ici votre commandant de "
                          "bord. Bienvenue à bord de {service} à "
                          "destination de {destination}. Nous "
                          "venons de nous stabiliser à {cruise}, et notre "
                          "temps de vol sera de {time}. Nous vous souhaitons "
                          "un agréable voyage.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "service": (
        (
            Line(PURSER, "Mesdames et messieurs, le personnel de cabine va "
                         "passer dans les allées pour vous proposer des "
                         "boissons et une collation.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Bonjour, désirez-vous quelque chose à boire ? Nous "
                        "avons du thé, du café et des jus de fruits.",
                 CABIN_PA, after_s=20.0),
        ),
    ),
    "service_cockpit": (
        (
            Line(PURSER, "Poste de pilotage, souhaitez-vous quelque chose ? "
                         "Le café est prêt.", INTERCOM),
        ),
    ),
    "cruise_chat": (
        (
            Line(FIRST_OFFICER, "Carburant conforme au plan de vol. Rien au "
                                "radar devant nous.", INTERCOM),
        ),
    ),
    "top_of_descent": (
        (
            Line(CAPTAIN, "Mesdames et messieurs, nous entamons notre "
                          "descente vers {destination}. Il y est "
                          "actuellement {local}, et la température est de "
                          "{temperature}. Personnel de cabine, préparez la "
                          "cabine pour l'atterrissage.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "ten_thousand_down": (
        (
            Line(FIRST_OFFICER, "Dix mille pieds.", INTERCOM),
            Line(PURSER, "Mesdames et messieurs, le signal lumineux est "
                         "allumé. Veuillez regagner votre place, attacher "
                         "votre ceinture, relever votre tablette et "
                         "redresser votre dossier.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_on", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "cabin_ready": (
        (
            Line(PURSER, "Personnel de cabine, veuillez vous asseoir pour "
                         "l'atterrissage.", CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabine parée pour l'atterrissage.",
                 INTERCOM, after_s=6.0),
        ),
    ),
    "approach": (
        (
            Line(FIRST_OFFICER, "Check atterrissage terminée. Train sorti, "
                                "trois vertes, volets sortis.", INTERCOM),
        ),
    ),
    "one_thousand": (
        (Line(FIRST_OFFICER, "Mille pieds. Stabilisé.", INTERCOM),),
    ),
    "five_hundred": ((Line(FIRST_OFFICER, "Cinq cents pieds.", INTERCOM),),),
    "minimums": ((Line(FIRST_OFFICER, "Minimums. Piste en vue.", INTERCOM),),),
    "touchdown": (
        (
            Line(FIRST_OFFICER, "Aérofreins sortis. Inverseurs verts.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Quatre-vingts nœuds.", INTERCOM, after_s=4.0),
            Line(FIRST_OFFICER, "Soixante nœuds. Inverseurs au ralenti.",
                 INTERCOM, after_s=3.0),
        ),
    ),
    "landed": (
        (
            Line(PURSER, "Mesdames et messieurs, bienvenue à {destination}. "
                         "Il est {local} et la température est de "
                         "{temperature}. Veuillez rester assis, ceinture "
                         "attachée, jusqu'à l'arrêt complet de l'appareil.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "taxi_in": (
        (
            Line(FIRST_OFFICER, "Check après atterrissage terminée.",
                 INTERCOM),
        ),
    ),
    "parked": (
        (
            Line(PURSER, "Personnel de cabine, désarmement des toboggans et "
                         "vérification croisée.", CABIN_PA, chime="down"),
            Line(PURSER, "Mesdames et messieurs, au nom de {airline} et de "
                         "tout l'équipage, nous vous remercions d'avoir volé "
                         "avec nous. N'oubliez aucun effet personnel à bord, "
                         "et à bientôt.",
                 CABIN_PA, after_s=7.0),
        ),
    ),
    "turbulence": (
        (
            Line(PURSER, "Mesdames et messieurs, le signal lumineux a été "
                         "rallumé. Veuillez regagner votre place et attacher "
                         "votre ceinture.",
                 CABIN_PA, chime="down"),
        ),
    ),
}


# --------------------------------------------------------------------------
# German
# --------------------------------------------------------------------------
#
# The standard callouts are translated rather than left in English. Real
# German airline crews call "eighty knots" and "V one" in English, because the
# operator's procedures are written that way -- but a pilot who has asked for
# a German first officer has asked to hear German, and the French script above
# already made that choice for the same reason. The beacon is
# "Rundumkennleuchte" rather than the English word the cockpit actually uses,
# because a German voice reading an English word is the thing this script
# exists to stop.

DE: dict[str, tuple[tuple[Line, ...], ...]] = {
    # ------------------------------------------------------------- gate ---
    "boarding": (
        (
            Line(PURSER, "Guten Tag, meine Damen und Herren, und herzlich "
                         "willkommen an Bord von {service} mit dem Ziel "
                         "{destination}. Bitte verstauen Sie Ihr Handgepäck "
                         "in den Gepäckfächern über Ihnen oder vollständig "
                         "unter dem Vordersitz und nehmen Sie zügig Ihre "
                         "Plätze ein.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(PURSER, "Meine Damen und Herren, willkommen an Bord von "
                         "{service} nach {destination}. Der Einstieg ist "
                         "fast abgeschlossen. Bitte begeben Sie sich zu "
                         "Ihrem Sitzplatz und verstauen Sie Ihr Gepäck über "
                         "Ihnen oder unter dem Vordersitz.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "doors_closed": (
        (
            Line(PURSER, "Kabinenpersonal, Türen auf automatik und "
                         "Gegenkontrolle.", CABIN_PA),
            Line(CABIN, "Türen sind scharf und gegenkontrolliert.",
                 CABIN_PA, after_s=5.0),
        ),
        (
            Line(PURSER, "Kabinenpersonal, Türen scharfschalten und "
                         "Gegenkontrolle.", CABIN_PA),
            Line(CABIN, "Scharf und gegenkontrolliert, danke.",
                 CABIN_PA, after_s=5.0),
        ),
    ),
    "pushback": (
        (
            Line(FIRST_OFFICER, "Bremsen gelöst, Push-back freigegeben.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Rundumkennleuchte ist an.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
        (
            Line(FIRST_OFFICER, "Feststellbremse gelöst, Zurückstoßen "
                                "genehmigt.", INTERCOM),
            Line(FIRST_OFFICER, "Rundumkennleuchte an, wir können "
                                "zurückstoßen.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
    ),
    "captain_welcome": (
        (
            Line(CAPTAIN, "Meine Damen und Herren, hier spricht Kapitän "
                          "{captain} aus dem Cockpit. Herzlich willkommen an "
                          "Bord von {service} nach {destination}. Wir werden "
                          "gerade von der Position zurückgestoßen, und "
                          "unsere Flugzeit beträgt heute etwa {time}. Bitte "
                          "nehmen Sie Ihre Plätze ein und legen Sie den "
                          "Sicherheitsgurt an.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Meine Damen und Herren, Kapitän {captain} und die "
                          "gesamte Besatzung begrüßen Sie an Bord von "
                          "{service} ab {origin}. Das Zurückstoßen läuft, "
                          "und wir starten in Kürze die Triebwerke. In "
                          "{destination} werden wir voraussichtlich in etwa "
                          "{time} sein.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Meine Damen und Herren, guten Tag aus dem "
                          "Cockpit, hier ist Kapitän {captain}. Wir werden "
                          "jetzt für den Flug nach {destination} "
                          "zurückgestoßen und sind rund {time} unterwegs. "
                          "Vielen Dank, dass Sie mit uns fliegen. Bitte "
                          "bleiben Sie während des Manövers angeschnallt "
                          "sitzen.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "cabin_welcome": (
        (
            Line(PURSER, "Meine Damen und Herren, im Namen von Kapitän "
                         "{captain} und der gesamten Besatzung begrüße "
                         "ich Sie an Bord von {service} nach {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Mein Name ist {purser}, ich bin heute für die "
                         "Kabine verantwortlich, und mit mir in der "
                         "Kabine sind "
                         "{crew}. Wenn Sie etwas brauchen, sprechen Sie uns "
                         "bitte jederzeit an.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Kurz nach dem Start kommen wir mit Getränken "
                         "durch die Kabine, im Anschluss servieren wir Ihnen "
                         "eine Mahlzeit.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Im Namen der Besatzung danken wir Ihnen, dass Sie "
                         "sich für {airline} entschieden haben, und "
                         "wünschen Ihnen einen angenehmen Flug.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Meine Damen und Herren, herzlich willkommen an "
                         "Bord von {service} nach {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Mein Name ist {purser}, ich leite heute die Kabine "
                         "auf diesem Flug, und für Sie da sind heute "
                         "außerdem {crew}.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Nach dem Start servieren wir Ihnen Getränke "
                         "und im weiteren Verlauf des Fluges eine Mahlzeit.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Vielen Dank, dass Sie sich für {airline} "
                         "entschieden haben. Wir wünschen Ihnen einen "
                         "angenehmen Flug.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "safety_briefing": (
        (
            Line(PURSER, "Meine Damen und Herren, wir bitten Sie für einige "
                         "Augenblicke um Ihre Aufmerksamkeit für die "
                         "Sicherheitshinweise an Bord dieses Flugzeugs.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Ihren Sicherheitsgurt schließen Sie so, und so "
                         "öffnen Sie ihn wieder. Bitte halten Sie ihn "
                         "geschlossen, solange Sie sitzen, flach und fest "
                         "über den Hüften.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Es gibt Ausgänge auf beiden Seiten der Kabine, und "
                         "der nächste kann sich hinter Ihnen befinden. Der "
                         "Weg dorthin ist am Boden markiert und leuchtet "
                         "auf, wenn die Kabinenbeleuchtung ausfällt.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Sollte der Kabinendruck abfallen, fällt eine Maske "
                         "vor Ihnen herunter. Ziehen Sie sie zu sich heran, "
                         "legen Sie sie über Mund und Nase und atmen Sie "
                         "ruhig weiter. Versorgen Sie zuerst sich selbst und "
                         "dann andere.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Ihre Schwimmweste befindet sich unter Ihrem Sitz. "
                         "Streifen Sie sie über den Kopf, schließen Sie die "
                         "Gurte vorne und blasen Sie sie erst außerhalb des "
                         "Flugzeugs auf.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Das Rauchen ist an Bord überall untersagt, auch in "
                         "den Toiletten, die mit Rauchmeldern ausgestattet "
                         "sind. Alle Hinweise finden Sie auf der "
                         "Sicherheitskarte in der Sitztasche vor Ihnen. Wir "
                         "bitten Sie, diese zu lesen. Vielen Dank.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Meine Damen und Herren, dürfen wir Sie um Ihre "
                         "Aufmerksamkeit für die Sicherheitshinweise bitten.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Bitte schließen Sie jetzt Ihren Sicherheitsgurt, "
                         "flach und fest über den Hüften, und halten Sie ihn "
                         "geschlossen, solange die Anschnallzeichen "
                         "leuchten.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Machen Sie sich bitte mit dem nächstgelegenen "
                         "Ausgang vertraut. Er kann sich auch hinter Ihnen "
                         "befinden. Die Bodenbeleuchtung weist Ihnen den Weg "
                         "dorthin.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Bei einem Abfall des Kabinendrucks erscheinen "
                         "Sauerstoffmasken über Ihnen. Ziehen Sie eine "
                         "kräftig zu sich heran, bedecken Sie Mund und Nase "
                         "und atmen Sie ruhig weiter.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Ihre Schwimmweste liegt unter Ihrem Sitz und wird "
                         "erst nach dem Verlassen des Flugzeugs aufgeblasen. "
                         "Alles Gezeigte finden Sie auf der Sicherheitskarte "
                         "vor Ihnen. Vielen Dank für Ihre Aufmerksamkeit.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "taxi": (
        (
            Line(FIRST_OFFICER, "Rollcheckliste abgeschlossen. Klappen "
                                "gesetzt, Steuerung überprüft.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Rollchecks erledigt, Klappen für den Start "
                                "gesetzt.", INTERCOM),
        ),
    ),
    "cabin_secure": (
        (
            Line(PURSER, "Kabinenpersonal, bitte für den Start Platz "
                         "nehmen.", CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Kabine ist gesichert.", INTERCOM,
                 after_s=6.0),
        ),
        (
            Line(PURSER, "Kabinenpersonal, bitte setzen Sie sich für den "
                         "Start.", CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Kabine gesichert, wir sind bereit.",
                 INTERCOM, after_s=6.0),
        ),
    ),
    "before_takeoff": (
        (
            Line(FIRST_OFFICER, "Aufrollcheckliste abgeschlossen. Piste "
                                "{runway}, Startfreigabe erhalten.",
                 INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Checkliste abgeschlossen, Piste {runway}. "
                                "Ihre Steuerung.", INTERCOM),
        ),
    ),
    # ---------------------------------------------------------- takeoff ---
    "eighty": ((Line(FIRST_OFFICER, "Achtzig Knoten.", INTERCOM,
                     needs="rolling"),),),
    "v_one": ((Line(FIRST_OFFICER, "V eins. Rotieren.", INTERCOM,
                    needs="rolling"),),),
    "positive_rate": (
        (
            Line(FIRST_OFFICER, "Steigrate positiv.", INTERCOM),
            Line(FIRST_OFFICER, "Fahrwerk einfahren.", INTERCOM, after_s=2.0,
                 needs="gear_down"),
        ),
    ),
    "after_takeoff": (
        (
            Line(FIRST_OFFICER, "Checkliste nach dem Start abgeschlossen.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Fahrwerk eingefahren, Klappen fahren "
                                "planmäßig ein.", INTERCOM,
                 after_s=1.5, needs="gear_up", patience_s=60.0),
        ),
        (
            Line(FIRST_OFFICER, "Checks nach dem Start abgeschlossen.",
                 INTERCOM),
        ),
    ),
    "ten_thousand_up": (
        (
            Line(FIRST_OFFICER, "Zehntausend Fuß.", INTERCOM),
            Line(PURSER, "Meine Damen und Herren, der Kapitän hat die "
                         "Anschnallzeichen ausgeschaltet. Sie können sich "
                         "nun in der Kabine bewegen. Wir bitten Sie dennoch, "
                         "den Gurt locker geschlossen zu halten, solange Sie "
                         "sitzen.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
        (
            Line(FIRST_OFFICER, "Zehntausend.", INTERCOM),
            Line(PURSER, "Meine Damen und Herren, die Anschnallzeichen sind "
                         "jetzt ausgeschaltet. Wir bitten Sie trotzdem, "
                         "Ihren Gurt geschlossen zu halten, wann immer Sie "
                         "auf Ihrem Platz sitzen, für den Fall unerwarteter "
                         "Turbulenzen.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    # ----------------------------------------------------------- cruise ---
    "welcome": (
        (
            Line(CAPTAIN, "Guten Tag, meine Damen und Herren, hier spricht "
                          "Kapitän {captain} aus dem Cockpit. Herzlich "
                          "willkommen an Bord von {service} auf dem Weg nach "
                          "{destination}. Wir haben soeben {cruise} "
                          "erreicht, unsere Flugzeit beträgt heute {time}, "
                          "und wir erwarten einen ruhigen Flug. Lehnen Sie "
                          "sich zurück und genießen Sie den Flug.",
                  CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Meine Damen und Herren, hier spricht Ihr Kapitän. "
                          "Wir fliegen jetzt in {cruise} in Richtung "
                          "{destination}, und wir werden noch etwa {time} "
                          "unterwegs sein. Die Kabinenbesatzung kommt in "
                          "Kürze durch die Kabine. Vielen Dank, dass Sie mit "
                          "uns fliegen.",
                  CABIN_PA, chime="up"),
        ),
    ),
    "service": (
        (
            Line(PURSER, "Meine Damen und Herren, unsere Kabinenbesatzung "
                         "kommt in Kürze mit einer Auswahl an Getränken und "
                         "kleinen Snacks durch die Kabine.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Guten Tag. Möchten Sie etwas trinken? Wir haben "
                        "Tee, Kaffee, Saft und Erfrischungsgetränke.",
                 CABIN_PA, after_s=20.0),
        ),
        (
            Line(PURSER, "Meine Damen und Herren, unser Kabinenservice "
                         "beginnt in Kürze. Wir kommen mit Getränken und "
                         "einem kleinen Imbiss durch die Kabine.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Etwas zu trinken für Sie? Wir haben Wasser, Saft, "
                        "Tee und Kaffee.",
                 CABIN_PA, after_s=20.0),
        ),
    ),
    "service_cockpit": (
        (
            Line(PURSER, "Cockpit, möchte einer von Ihnen etwas? Wir haben "
                         "Kaffee aufgesetzt.", INTERCOM),
        ),
        (
            Line(PURSER, "Etwas aus der Bordküche für das Cockpit?",
                 INTERCOM),
        ),
    ),
    "cruise_chat": (
        (
            Line(FIRST_OFFICER, "Der Treibstoff liegt im Plan. Auf dem "
                                "Wetterradar ist nichts vor uns.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Wir sind ein paar Minuten vor dem Flugplan, "
                                "und der Flug bleibt ruhig.", INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Alle Systeme normal, Treibstoffkontrolle in "
                                "Ordnung.", INTERCOM),
        ),
    ),
    # ---------------------------------------------------------- descent ---
    "top_of_descent": (
        (
            Line(CAPTAIN, "Meine Damen und Herren, wir haben mit dem "
                          "Sinkflug nach {destination} begonnen. Die "
                          "Ortszeit dort ist {local} und die Temperatur "
                          "beträgt {temperature}. Kabinenpersonal, bitte die "
                          "Kabine für die Landung vorbereiten.",
                  CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Meine Damen und Herren, wir haben unseren "
                          "Sinkflug in Richtung {destination} begonnen. Dort "
                          "beträgt die Temperatur {temperature}, und die "
                          "Ortszeit ist {local}. Kabinenpersonal, bitte die "
                          "Kabine für die Ankunft vorbereiten.",
                  CABIN_PA, chime="up"),
        ),
    ),
    "ten_thousand_down": (
        (
            Line(FIRST_OFFICER, "Zehntausend Fuß. Lichter an.", INTERCOM),
            Line(PURSER, "Meine Damen und Herren, die Anschnallzeichen sind "
                         "jetzt eingeschaltet. Bitte kehren Sie auf Ihren "
                         "Platz zurück, schließen Sie Ihren Gurt und achten "
                         "Sie darauf, dass Ihr Tischchen verstaut und Ihre "
                         "Rückenlehne aufrecht ist.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_on", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "cabin_ready": (
        (
            Line(PURSER, "Kabinenpersonal, bitte für die Landung Platz "
                         "nehmen.", CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Kabine ist für die Landung gesichert.",
                 INTERCOM, after_s=6.0),
        ),
        (
            Line(PURSER, "Kabine gesichert. Besatzung, bitte Platz nehmen.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Die Kabine meldet gesichert.", INTERCOM,
                 after_s=6.0),
        ),
    ),
    "approach": (
        (
            Line(FIRST_OFFICER, "Landecheckliste abgeschlossen. Fahrwerk "
                                "ausgefahren, drei grün, Klappen gesetzt.",
                 INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Landechecks abgeschlossen. Fahrwerk "
                                "ausgefahren und verriegelt.", INTERCOM),
        ),
    ),
    "one_thousand": ((Line(FIRST_OFFICER, "Eintausend. Stabilisiert.",
                           INTERCOM),),),
    "five_hundred": ((Line(FIRST_OFFICER, "Fünfhundert.", INTERCOM),),),
    "minimums": ((Line(FIRST_OFFICER, "Minimum. Piste in Sicht.",
                       INTERCOM),),),
    "touchdown": (
        (
            Line(FIRST_OFFICER, "Störklappen ausgefahren. Umkehrschub grün.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Achtzig Knoten.", INTERCOM, after_s=4.0),
            Line(FIRST_OFFICER, "Sechzig Knoten. Umkehrschub auf Leerlauf.",
                 INTERCOM, after_s=3.0),
        ),
    ),
    # ------------------------------------------------------------- ramp ---
    "landed": (
        (
            Line(PURSER, "Meine Damen und Herren, willkommen in "
                         "{destination}. Die Ortszeit ist {local} und die "
                         "Temperatur beträgt {temperature}. Bitte bleiben "
                         "Sie mit geschlossenem Gurt sitzen, bis das "
                         "Flugzeug vollständig zum Stillstand gekommen ist "
                         "und die Anschnallzeichen ausgeschaltet sind.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(PURSER, "Meine Damen und Herren, willkommen in "
                         "{destination}. Die Ortszeit ist {local}. Bitte "
                         "bleiben Sie mit geschlossenem Gurt sitzen, bis wir "
                         "die Parkposition erreicht haben und die Zeichen "
                         "erlöschen.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "taxi_in": (
        (
            Line(FIRST_OFFICER, "Checkliste nach der Landung abgeschlossen. "
                                "Klappen eingefahren, Störklappen verstaut.",
                 INTERCOM),
        ),
        (
            Line(FIRST_OFFICER, "Checks nach der Landung abgeschlossen.",
                 INTERCOM),
        ),
    ),
    "parked": (
        (
            Line(PURSER, "Kabinenpersonal, Türen auf manuell und "
                         "Gegenkontrolle.", CABIN_PA, chime="down"),
            Line(PURSER, "Meine Damen und Herren, im Namen von {airline} und "
                         "der gesamten Besatzung danken wir Ihnen, dass Sie "
                         "heute mit uns geflogen sind. Bitte nehmen Sie alle "
                         "Ihre persönlichen Gegenstände mit, und wir hoffen, "
                         "Sie bald wieder an Bord begrüßen zu dürfen.",
                 CABIN_PA, after_s=7.0),
        ),
        (
            Line(PURSER, "Kabinenpersonal, Türen entschärfen und "
                         "Gegenkontrolle.", CABIN_PA, chime="down"),
            Line(PURSER, "Meine Damen und Herren, vielen Dank, dass Sie sich "
                         "für {airline} entschieden haben. Bitte sehen Sie "
                         "sich um, ob Sie alle Ihre persönlichen Gegenstände "
                         "haben, und wir wünschen Ihnen eine angenehme "
                         "Weiterreise.",
                 CABIN_PA, after_s=7.0),
        ),
    ),
    "turbulence": (
        (
            Line(PURSER, "Meine Damen und Herren, der Kapitän hat die "
                         "Anschnallzeichen wieder eingeschaltet. Bitte "
                         "kehren Sie auf Ihren Platz zurück und schließen "
                         "Sie Ihren Gurt.",
                 CABIN_PA, chime="down"),
        ),
    ),
}


# --------------------------------------------------------------------------
# Dutch
# --------------------------------------------------------------------------
#
# Written for Brussels Airlines, KLM and Transavia, whose cabins read Dutch
# before anything else. Paragraph for paragraph the French script, so a
# Belgian crew reading Dutch, then French, then English pair each part of the
# safety demonstration in all three rather than reading three announcements
# back to back. The callouts are Dutch for the reason the German ones are
# German: a pilot who has a Dutch first officer has asked to hear Dutch.

NL: dict[str, tuple[tuple[Line, ...], ...]] = {
    "boarding": (
        (
            Line(PURSER, "Dames en heren, welkom aan boord van {service} "
                         "naar {destination}. Wilt u uw handbagage in de "
                         "bagagerekken of onder de stoel voor u opbergen, en "
                         "zo snel mogelijk uw plaats innemen.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "doors_closed": (
        (
            Line(PURSER, "Cabinepersoneel, glijbanen inschakelen en "
                         "kruiscontrole.", CABIN_PA),
            Line(CABIN, "Glijbanen ingeschakeld en gecontroleerd.",
                 CABIN_PA, after_s=5.0),
        ),
    ),
    "pushback": (
        (
            Line(FIRST_OFFICER, "Parkeerrem los, pushback goedgekeurd.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Zwaailicht aan.", INTERCOM,
                 after_s=1.5, needs="beacon_on", patience_s=45.0),
        ),
    ),
    "captain_welcome": (
        (
            Line(CAPTAIN, "Dames en heren, hier spreekt uw gezagvoerder "
                          "{captain}. Welkom aan boord van {service} naar "
                          "{destination}. We vertrekken nu van de "
                          "parkeerplaats, en de vliegtijd bedraagt ongeveer "
                          "{time}. Wilt u gaan zitten en uw gordel vastmaken. "
                          "Een goede vlucht gewenst.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Dames en heren, gezagvoerder {captain} en de hele "
                          "bemanning heten u van harte welkom aan boord van "
                          "{service} vanuit {origin}. We worden nu "
                          "achteruitgeduwd en starten zo dadelijk de motoren. "
                          "We verwachten over ongeveer {time} in "
                          "{destination} aan te komen.",
                 CABIN_PA, chime="up"),
        ),
        (
            Line(CAPTAIN, "Goedendag dames en heren, met uw gezagvoerder "
                          "{captain}. We worden op dit moment achteruitgeduwd "
                          "voor onze vlucht naar {destination}, die ongeveer "
                          "{time} duurt. Dank u wel dat u met ons vliegt, en "
                          "blijft u tijdens het manoeuvreren zitten met de "
                          "gordel vast.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "cabin_welcome": (
        (
            Line(PURSER, "Dames en heren, namens gezagvoerder {captain} en "
                         "de hele bemanning, welkom aan boord van {service} "
                         "naar {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Mijn naam is {purser}, ik ben uw purser op deze "
                         "vlucht, en vandaag zijn met mij in de cabine "
                         "{crew}. Aarzel niet om ons aan te spreken als u "
                         "iets nodig heeft.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Kort na het opstijgen komen we langs met dranken, "
                         "en daarna serveren we u een maaltijd.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Namens de hele bemanning, dank u wel dat u voor "
                         "{airline} hebt gekozen. Wij wensen u een "
                         "uitstekende vlucht.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
        (
            Line(PURSER, "Dames en heren, welkom aan boord van {service}, "
                         "met bestemming {destination}.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Ik ben {purser}, de purser, en de bemanning die u "
                         "vandaag begeleidt bestaat uit {crew}.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Na het opstijgen bieden we u dranken aan, gevolgd "
                         "door een maaltijd tijdens de vlucht.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Dank u wel voor uw keuze voor {airline}, en een "
                         "hele fijne vlucht.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "safety_briefing": (
        (
            Line(PURSER, "Dames en heren, mogen wij even uw aandacht voor de "
                         "veiligheidsinstructies.",
                 CABIN_PA, chime="up"),
            Line(PURSER, "Uw gordel maakt u zo vast en zo weer los. Houd hem "
                         "vast zolang u zit, strak over uw heupen.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Dit toestel heeft nooduitgangen aan beide kanten "
                         "van de cabine. De dichtstbijzijnde kan achter u "
                         "liggen, en de weg ernaartoe is op de vloer "
                         "gemarkeerd.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Bij drukverlies valt er een zuurstofmasker voor u "
                         "naar beneden. Trek het naar u toe, plaats het over "
                         "neus en mond, en adem normaal.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
            Line(PURSER, "Uw reddingsvest bevindt zich onder uw stoel. Blaas "
                         "het pas op wanneer u het toestel verlaten hebt. "
                         "Roken is aan boord niet toegestaan. Dank u wel "
                         "voor uw aandacht.",
                 CABIN_PA, after_s=BRIEFING_BREATH),
        ),
    ),
    "taxi": (
        (
            Line(FIRST_OFFICER, "Taxichecklist voltooid. Kleppen uit, "
                                "besturing gecontroleerd.", INTERCOM),
        ),
    ),
    "cabin_secure": (
        (
            Line(PURSER, "Cabinepersoneel, neem plaats voor het opstijgen.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabine gereed.", INTERCOM, after_s=6.0),
        ),
    ),
    "before_takeoff": (
        (
            Line(FIRST_OFFICER, "Checklist voor het opstijgen voltooid. Baan "
                                "{runway}, vrij om op te stijgen.",
                 INTERCOM),
        ),
    ),
    "eighty": ((Line(FIRST_OFFICER, "Tachtig knopen.", INTERCOM,
                     needs="rolling"),),),
    "v_one": ((Line(FIRST_OFFICER, "V een. Roteren.", INTERCOM,
                    needs="rolling"),),),
    "positive_rate": (
        (
            Line(FIRST_OFFICER, "Positieve klimsnelheid.", INTERCOM),
            Line(FIRST_OFFICER, "Landingsgestel op.", INTERCOM, after_s=2.0,
                 needs="gear_down"),
        ),
    ),
    "after_takeoff": (
        (
            Line(FIRST_OFFICER, "Checklist na het opstijgen voltooid.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Landingsgestel op, kleppen in.", INTERCOM,
                 after_s=1.5, needs="gear_up", patience_s=60.0),
        ),
    ),
    "ten_thousand_up": (
        (
            Line(FIRST_OFFICER, "Tienduizend voet.", INTERCOM),
            Line(PURSER, "Dames en heren, de gezagvoerder heeft de "
                         "gordelsignalen gedoofd. We raden u toch aan uw "
                         "gordel vast te houden zolang u zit.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_off", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "welcome": (
        (
            Line(CAPTAIN, "Dames en heren, hier spreekt uw gezagvoerder. "
                          "Welkom aan boord van {service} naar "
                          "{destination}. We hebben zojuist {cruise} bereikt, "
                          "en de vliegtijd bedraagt {time}. Wij wensen u een "
                          "aangename reis.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "service": (
        (
            Line(PURSER, "Dames en heren, het cabinepersoneel komt zo "
                         "dadelijk langs met dranken en een snack.",
                 CABIN_PA, chime="up"),
            Line(CABIN, "Goedendag, wilt u iets drinken? We hebben thee, "
                        "koffie en vruchtensap.",
                 CABIN_PA, after_s=20.0),
        ),
    ),
    "service_cockpit": (
        (
            Line(PURSER, "Cockpit, wilt u iets hebben? De koffie is klaar.",
                 INTERCOM),
        ),
    ),
    "cruise_chat": (
        (
            Line(FIRST_OFFICER, "Brandstof volgens vliegplan. Niets op de "
                                "radar voor ons.", INTERCOM),
        ),
    ),
    "top_of_descent": (
        (
            Line(CAPTAIN, "Dames en heren, we beginnen aan onze daling naar "
                          "{destination}. Het is daar nu {local}, en de "
                          "temperatuur is {temperature}. Cabinepersoneel, "
                          "maak de cabine gereed voor de landing.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "ten_thousand_down": (
        (
            Line(FIRST_OFFICER, "Tienduizend voet.", INTERCOM),
            Line(PURSER, "Dames en heren, de gordelsignalen zijn "
                         "ingeschakeld. Wilt u terugkeren naar uw plaats, uw "
                         "gordel vastmaken, uw tafeltje inklappen en uw "
                         "rugleuning rechtop zetten.",
                 CABIN_PA, chime="down", after_s=4.0,
                 needs="signs_on", patience_s=SWITCH_PATIENCE_S),
        ),
    ),
    "cabin_ready": (
        (
            Line(PURSER, "Cabinepersoneel, neem plaats voor de landing.",
                 CABIN_PA, chime="up"),
            Line(FIRST_OFFICER, "Cabine gereed voor de landing.",
                 INTERCOM, after_s=6.0),
        ),
    ),
    "approach": (
        (
            Line(FIRST_OFFICER, "Landingschecklist voltooid. Landingsgestel "
                                "uit, drie groen, kleppen uit.", INTERCOM),
        ),
    ),
    "one_thousand": (
        (Line(FIRST_OFFICER, "Duizend voet. Gestabiliseerd.", INTERCOM),),
    ),
    "five_hundred": ((Line(FIRST_OFFICER, "Vijfhonderd voet.", INTERCOM),),),
    "minimums": ((Line(FIRST_OFFICER, "Minimum. Baan in zicht.",
                       INTERCOM),),),
    "touchdown": (
        (
            Line(FIRST_OFFICER, "Spoilers uit. Straalomkeerders groen.",
                 INTERCOM),
            Line(FIRST_OFFICER, "Tachtig knopen.", INTERCOM, after_s=4.0),
            Line(FIRST_OFFICER, "Zestig knopen. Straalomkeerders stationair.",
                 INTERCOM, after_s=3.0),
        ),
    ),
    "landed": (
        (
            Line(PURSER, "Dames en heren, welkom in {destination}. Het is "
                         "{local} en de temperatuur is {temperature}. Blijft "
                         "u zitten met de gordel vast tot het toestel "
                         "volledig tot stilstand is gekomen.",
                 CABIN_PA, chime="up"),
        ),
    ),
    "taxi_in": (
        (
            Line(FIRST_OFFICER, "Checklist na de landing voltooid.",
                 INTERCOM),
        ),
    ),
    "parked": (
        (
            Line(PURSER, "Cabinepersoneel, glijbanen uitschakelen en "
                         "kruiscontrole.", CABIN_PA, chime="down"),
            Line(PURSER, "Dames en heren, namens {airline} en de hele "
                         "bemanning danken wij u dat u met ons hebt "
                         "gevlogen. Vergeet uw persoonlijke bezittingen niet, "
                         "en tot ziens.",
                 CABIN_PA, after_s=7.0),
        ),
    ),
    "turbulence": (
        (
            Line(PURSER, "Dames en heren, de gordelsignalen zijn weer "
                         "ingeschakeld. Wilt u terugkeren naar uw plaats en "
                         "uw gordel vastmaken.",
                 CABIN_PA, chime="down"),
        ),
    ),
}


SCRIPTS: dict[str, dict[str, tuple[tuple[Line, ...], ...]]] = {
    "en": EN,
    "fr": FR,
    "de": DE,
    "nl": NL,
}


# --------------------------------------------------------------------------
# answering the flight deck
# --------------------------------------------------------------------------
#
# Everything above is somebody speaking because the flight reached a point.
# This is the other half: somebody speaking because they were spoken to.
#
# The key is what :mod:`wilcoatc.atc.interphone` made of the transmission,
# with the half of the aeroplane it was said to on the front, so that one
# table covers a purser taking a drinks order and a headset acknowledging a
# push. Both are answers to the same microphone.
#
# A cabin answer is on the interphone, because it was said to the flight deck
# and not to the passengers -- with one exception, which is the pair of
# requests that are themselves instructions to make an announcement. Those
# answer on the interphone and then say the thing in the cabin, which is what
# actually happens: the purser says "standing by" to you and then picks up the
# handset for everybody else.

ANSWERS: dict[str, dict[str, tuple[tuple[Line, ...], ...]]] = {
    "en": {
        "cabin_attention": (
            (Line(PURSER, "Flight deck, go ahead.", INTERCOM),),
            (Line(PURSER, "Yes captain, what can I get you?", INTERCOM),),
            (Line(PURSER, "Cabin, listening.", INTERCOM),),
        ),
        "cabin_drink": (
            (
                Line(PURSER, "Of course. I will bring it up in a moment.",
                     INTERCOM),
                # Long enough that it is somebody walking up the aisle with a
                # tray rather than a second half of the same sentence.
                Line(PURSER, "Here you are. Mind the cup, it is hot.",
                     INTERCOM, after_s=75.0),
            ),
            (
                Line(PURSER, "Certainly captain, coming right up.", INTERCOM),
                Line(PURSER, "There you go. Anything else while I am here?",
                     INTERCOM, after_s=75.0),
            ),
        ),
        "cabin_meal": (
            (Line(PURSER, "I will bring your trays up as soon as the cabin "
                          "service is finished.", INTERCOM),),
            (Line(PURSER, "Crew meals are ready. I will bring them forward "
                          "once we have served the cabin.", INTERCOM),),
        ),
        "cabin_status": (
            (Line(PURSER, "All good back here. Everybody is seated and the "
                          "cabin is tidy.", INTERCOM),),
            (Line(PURSER, "Cabin is quiet, no problems to report.",
                  INTERCOM),),
            (Line(PURSER, "We are almost finished in the cabin. Nothing for "
                          "you to worry about.", INTERCOM),),
        ),
        "cabin_secure": (
            (
                Line(PURSER, "Understood. We will secure the cabin now.",
                     INTERCOM),
                Line(PURSER, "Cabin crew, please prepare the cabin and take "
                             "your seats.", CABIN_PA, chime="up",
                     after_s=4.0),
            ),
        ),
        "cabin_turbulence": (
            (
                Line(PURSER, "Understood, we will sit down.", INTERCOM),
                Line(PURSER, "Ladies and gentlemen, the captain has switched "
                             "the seatbelt signs back on. Please return to "
                             "your seat and fasten your seatbelt.",
                     CABIN_PA, chime="down", after_s=4.0),
            ),
        ),
        "cabin_thanks": (
            (Line(PURSER, "You are very welcome.", INTERCOM),),
            (Line(PURSER, "Any time. Just call.", INTERCOM),),
        ),
        # -- the man on the headset ---------------------------------------
        "ground_attention": (
            (Line(GROUND, "Flight deck, ground. Go ahead.", INTERCOM),),
            (Line(GROUND, "Ground here, standing by.", INTERCOM),),
        ),
        "ground_pushback": (
            (Line(GROUND, "Roger, calling the tug. Release the parking brake "
                          "when you are ready.", INTERCOM),),
            (Line(GROUND, "Pushback on its way. Brakes off when you have the "
                          "clearance.", INTERCOM),),
        ),
        "ground_stop_pushback": (
            (Line(GROUND, "Stopping the push. Set the parking brake.",
                  INTERCOM),),
        ),
        "ground_confirm": (
            (Line(GROUND, "Confirmed. Brakes released, cleared to push.",
                  INTERCOM),),
            (Line(GROUND, "Understood, we are pushing.", INTERCOM),),
        ),
        "ground_boarding": (
            (Line(GROUND, "Roger, we will start boarding.", INTERCOM),),
        ),
        "ground_deboarding": (
            (Line(GROUND, "Roger, opening up and starting disembarkation.",
                  INTERCOM),),
        ),
        "ground_refuel": (
            (Line(GROUND, "Roger, the fuel truck is on its way.", INTERCOM),),
        ),
        "ground_catering": (
            (Line(GROUND, "Roger, catering has been called.", INTERCOM),),
        ),
        "ground_jetway": (
            (Line(GROUND, "Roger, operating the jetway now.", INTERCOM),),
        ),
        "ground_stairs": (
            (Line(GROUND, "Roger, bringing the stairs round.", INTERCOM),),
        ),
        "ground_deice": (
            (Line(GROUND, "Roger, de-icing has been requested.", INTERCOM),),
        ),
        "ground_gpu": (
            (Line(GROUND, "Roger, ground power coming.", INTERCOM),),
        ),
        # Said instead of any of the above when nothing could actually be
        # asked for. The ramp is the one place in this program where the
        # reply is a promise about the world outside it, so a promise that
        # could not be kept is not made.
        "ground_none": (
            (Line(GROUND, "Sorry, we cannot do that from here.", INTERCOM),),
        ),
        "ground_status": (
            (Line(GROUND, "Standing by. Nothing holding you up from our "
                          "side.", INTERCOM),),
        ),
    },
    "fr": {
        "cabin_attention": (
            (Line(PURSER, "Cockpit, je vous écoute.", INTERCOM),),
            (Line(PURSER, "Oui commandant, que puis-je vous apporter ?",
                  INTERCOM),),
        ),
        "cabin_drink": (
            (
                Line(PURSER, "Bien sûr, je vous apporte cela tout de suite.",
                     INTERCOM),
                Line(PURSER, "Voilà. Attention, c'est chaud.", INTERCOM,
                     after_s=75.0),
            ),
        ),
        "cabin_meal": (
            (Line(PURSER, "Je vous monte les plateaux dès que le service en "
                          "cabine sera terminé.", INTERCOM),),
        ),
        "cabin_status": (
            (Line(PURSER, "Tout va bien à l'arrière. Les passagers sont "
                          "installés et la cabine est en ordre.", INTERCOM),),
        ),
        "cabin_secure": (
            (
                Line(PURSER, "Bien reçu, nous sécurisons la cabine.",
                     INTERCOM),
                Line(PURSER, "Personnel de cabine, veuillez préparer la "
                             "cabine et vous asseoir.", CABIN_PA,
                     chime="up", after_s=4.0),
            ),
        ),
        "cabin_turbulence": (
            (
                Line(PURSER, "Bien reçu, nous nous asseyons.", INTERCOM),
                Line(PURSER, "Mesdames et messieurs, le commandant vient de "
                             "rallumer les consignes lumineuses. Regagnez "
                             "votre siège et attachez votre ceinture.",
                     CABIN_PA, chime="down", after_s=4.0),
            ),
        ),
        "cabin_thanks": (
            (Line(PURSER, "Je vous en prie.", INTERCOM),),
        ),
        "ground_attention": (
            (Line(GROUND, "Cockpit, personnel au sol, je vous écoute.",
                  INTERCOM),),
        ),
        "ground_pushback": (
            (Line(GROUND, "Bien reçu, j'appelle le tracteur. Desserrez le "
                          "frein de parc quand vous êtes prêts.", INTERCOM),),
        ),
        "ground_stop_pushback": (
            (Line(GROUND, "On arrête le repoussage. Frein de parc, s'il vous "
                          "plaît.", INTERCOM),),
        ),
        "ground_confirm": (
            (Line(GROUND, "Confirmé. Frein desserré, on repousse.",
                  INTERCOM),),
        ),
        "ground_boarding": (
            (Line(GROUND, "Bien reçu, nous commençons l'embarquement.",
                  INTERCOM),),
        ),
        "ground_deboarding": (
            (Line(GROUND, "Bien reçu, nous commençons le débarquement.",
                  INTERCOM),),
        ),
        "ground_refuel": (
            (Line(GROUND, "Bien reçu, l'avitailleur arrive.", INTERCOM),),
        ),
        "ground_catering": (
            (Line(GROUND, "Bien reçu, le commissariat est demandé.",
                  INTERCOM),),
        ),
        "ground_jetway": (
            (Line(GROUND, "Bien reçu, nous manœuvrons la passerelle.",
                  INTERCOM),),
        ),
        "ground_stairs": (
            (Line(GROUND, "Bien reçu, nous amenons l'escalier.", INTERCOM),),
        ),
        "ground_deice": (
            (Line(GROUND, "Bien reçu, le dégivrage est demandé.", INTERCOM),),
        ),
        "ground_gpu": (
            (Line(GROUND, "Bien reçu, groupe de parc en route.", INTERCOM),),
        ),
        "ground_none": (
            (Line(GROUND, "Désolé, nous ne pouvons pas faire cela d'ici.",
                  INTERCOM),),
        ),
        "ground_status": (
            (Line(GROUND, "En attente. Rien ne vous retient de notre côté.",
                  INTERCOM),),
        ),
    },
    "de": {
        "cabin_attention": (
            (Line(PURSER, "Cockpit, sprechen Sie.", INTERCOM),),
            (Line(PURSER, "Ja Kapitän, was darf ich Ihnen bringen?",
                  INTERCOM),),
        ),
        "cabin_drink": (
            (
                Line(PURSER, "Natürlich, ich bringe es gleich nach vorne.",
                     INTERCOM),
                Line(PURSER, "Bitte sehr. Vorsicht, es ist heiß.", INTERCOM,
                     after_s=75.0),
            ),
        ),
        "cabin_meal": (
            (Line(PURSER, "Ich bringe Ihnen die Tabletts, sobald der Service "
                          "in der Kabine beendet ist.", INTERCOM),),
        ),
        "cabin_status": (
            (Line(PURSER, "Hinten ist alles in Ordnung. Alle sitzen, die "
                          "Kabine ist aufgeräumt.", INTERCOM),),
        ),
        "cabin_secure": (
            (
                Line(PURSER, "Verstanden, wir sichern die Kabine.", INTERCOM),
                Line(PURSER, "Kabinenpersonal, bitte die Kabine vorbereiten "
                             "und Platz nehmen.", CABIN_PA, chime="up",
                     after_s=4.0),
            ),
        ),
        "cabin_turbulence": (
            (
                Line(PURSER, "Verstanden, wir setzen uns.", INTERCOM),
                Line(PURSER, "Meine Damen und Herren, der Kapitän hat die "
                             "Anschnallzeichen wieder eingeschaltet. Bitte "
                             "kehren Sie auf Ihren Platz zurück und "
                             "schließen Sie Ihren Gurt.", CABIN_PA,
                     chime="down", after_s=4.0),
            ),
        ),
        "cabin_thanks": (
            (Line(PURSER, "Sehr gerne.", INTERCOM),),
        ),
        "ground_attention": (
            (Line(GROUND, "Cockpit, Bodenpersonal. Sprechen Sie.",
                  INTERCOM),),
        ),
        "ground_pushback": (
            (Line(GROUND, "Verstanden, ich rufe den Schlepper. Lösen Sie die "
                          "Parkbremse, wenn Sie bereit sind.", INTERCOM),),
        ),
        "ground_stop_pushback": (
            (Line(GROUND, "Wir stoppen den Pushback. Parkbremse bitte.",
                  INTERCOM),),
        ),
        "ground_confirm": (
            (Line(GROUND, "Bestätigt. Bremse gelöst, wir schieben.",
                  INTERCOM),),
        ),
        "ground_boarding": (
            (Line(GROUND, "Verstanden, wir beginnen mit dem Boarding.",
                  INTERCOM),),
        ),
        "ground_deboarding": (
            (Line(GROUND, "Verstanden, wir beginnen mit dem Aussteigen.",
                  INTERCOM),),
        ),
        "ground_refuel": (
            (Line(GROUND, "Verstanden, der Tankwagen kommt.", INTERCOM),),
        ),
        "ground_catering": (
            (Line(GROUND, "Verstanden, das Catering ist angefordert.",
                  INTERCOM),),
        ),
        "ground_jetway": (
            (Line(GROUND, "Verstanden, wir fahren die Brücke an.",
                  INTERCOM),),
        ),
        "ground_stairs": (
            (Line(GROUND, "Verstanden, die Treppe kommt.", INTERCOM),),
        ),
        "ground_deice": (
            (Line(GROUND, "Verstanden, die Enteisung ist angefordert.",
                  INTERCOM),),
        ),
        "ground_gpu": (
            (Line(GROUND, "Verstanden, Bodenstrom kommt.", INTERCOM),),
        ),
        "ground_none": (
            (Line(GROUND, "Tut mir leid, das können wir von hier nicht "
                          "machen.", INTERCOM),),
        ),
        "ground_status": (
            (Line(GROUND, "Wir warten. Von unserer Seite hält Sie nichts "
                          "auf.", INTERCOM),),
        ),
    },
}


# Which switch each cue belongs to, so a pilot who wants the callouts and
# nothing else gets exactly that.
CUE_GROUPS: dict[str, str] = {
    "boarding": "announcements",
    "doors_closed": "briefings",
    "pushback": "checklists",
    "captain_welcome": "announcements",
    "cabin_welcome": "announcements",
    "safety_briefing": "briefings",
    "taxi": "checklists",
    "cabin_secure": "briefings",
    "before_takeoff": "checklists",
    "eighty": "callouts",
    "v_one": "callouts",
    "positive_rate": "callouts",
    "after_takeoff": "checklists",
    "ten_thousand_up": "callouts",
    "welcome": "announcements",
    "service": "service",
    "service_cockpit": "service",
    "cruise_chat": "chatter",
    "top_of_descent": "announcements",
    "ten_thousand_down": "callouts",
    "cabin_ready": "briefings",
    "approach": "checklists",
    "one_thousand": "callouts",
    "five_hundred": "callouts",
    "minimums": "callouts",
    "touchdown": "callouts",
    "landed": "announcements",
    "taxi_in": "checklists",
    "parked": "announcements",
    "turbulence": "announcements",
}

# The recorded announcement each cue corresponds to, by the names the Fenix
# A320 uses for its cabin sound packs.
#
# There are a lot of those packs about, and reading them is the difference
# between a synthesised purser and the pilot's own airline. Where a recording
# exists for a cue it replaces the written lines entirely -- one file is one
# announcement, and half a recording followed by half a synthesised one would
# be worse than either.
#
# The moments are this program's own rather than Fenix's. Their AfterTakeoff
# fires at three thousand feet and this one at ten, because ten thousand is
# where the signs and the lights change and that is what the announcement is
# about. It is the same announcement, said once, at the point this program
# already had for it.
CUE_RECORDINGS: dict[str, str] = {
    "boarding": "BoardingWelcome",
    # Not a name the Fenix packs use -- they have no captain in them, because
    # in that aeroplane the captain is the person flying it. Here there is
    # one, so the moment exists, and it is named the way the rest of the
    # table is named so that a pilot who records their own can drop it in
    # beside the others.
    "captain_welcome": "CaptainWelcome",
    # Likewise not a Fenix name: the moment exists here because the
    # cabin manager introduces themselves, and a pilot who records
    # their own drops it in beside the rest.
    "cabin_welcome": "CabinWelcome",
    "doors_closed": "ArmDoors",
    "safety_briefing": "SafetyBriefing",
    "cabin_secure": "CrewSeatsTakeoff",
    "ten_thousand_up": "AfterTakeoff",
    "turbulence": "FastenSeatbelt",
    "ten_thousand_down": "DescentSeatbelts",
    "cabin_ready": "CrewSeatsLanding",
    "landed": "AfterLanding",
    "parked": "DisarmDoors",
}

# Moments a pack has and this program's written crew do not. They happen only
# when there is a recording for them: a cabin that dims the lights for a night
# departure is a thing a recording says and a text template cannot.
#
# ``boarding_music`` is the exception, and it is the only cue in the file that
# is not words at all. A recording still wins -- somebody who has put their
# airline's own boarding music in the folder has said what they want -- but
# where there is none the cabin now plays a synthesised bed instead of
# nothing, from :mod:`wilcoatc.audio.boarding`. That is a reversal: this used
# to say that inventing boarding music would be silly, on the grounds that
# the alternative to a licensed track is an embarrassing imitation of one.
# What changed is that nobody is imitating a track. The bed is four chords
# under the threshold of attention, and the thing it replaces is not music,
# it is a completely silent aeroplane full of people.
#
# Each is a cue in its own right, so the switches and the once-per-flight
# rule apply to them exactly as they do to everything else.
RECORDED_ONLY: dict[str, str] = {
    "boarding_music": "BoardingMusic",
    "boarding_complete": "BoardingComplete",
    "pre_safety_briefing": "PreSafetyBriefing",
    "cabin_dim": "CabinDimTakeoff",
    "cabin_secure_reply": "CallCabinSecureTakeoff",
    "cabin_ready_reply": "CallCabinSecureLanding",
    "disembark": "DisembarkStarted",
}

# Which switch each of those belongs to, and which half of the aeroplane.
for _cue, _group in (("boarding_music", "announcements"),
                     ("boarding_complete", "announcements"),
                     ("pre_safety_briefing", "briefings"),
                     ("cabin_dim", "briefings"),
                     ("cabin_secure_reply", "briefings"),
                     ("cabin_ready_reply", "briefings"),
                     ("disembark", "announcements")):
    CUE_GROUPS[_cue] = _group
del _cue, _group


# Which half of the aeroplane a group belongs to. The cabin can be silenced
# without losing the first officer, which is the split most people want.
GROUP_SIDE: dict[str, str] = {
    "callouts": "copilot",
    "checklists": "copilot",
    "chatter": "copilot",
    "briefings": "cabin",
    "announcements": "cabin",
    "service": "cabin",
}


__all__ = [
    "Line", "SCRIPTS", "ANSWERS", "EN", "FR", "DE", "NL", "CUE_GROUPS",
    "GROUP_SIDE", "CUE_RECORDINGS", "RECORDED_ONLY",
    "FIRST_OFFICER", "CAPTAIN", "PURSER", "CABIN", "GROUND", "INTERCOM",
    "CABIN_PA",
]
