Recorded cabin announcements
============================

Put audio files in the Announcements folder beside this one and WilcoATC will
play them instead of its synthesised cabin crew.

    announcements/
      Announcements/
        BoardingWelcome.ogg          played for any flight
        SafetyBriefing.ogg
        BAW/                         played only when the callsign is BAW...
          BoardingWelcome.ogg
        EJU/
          BoardingWelcome.ogg

The layout and the file names are the ones the Fenix A320 uses, so a sound
pack made for that aeroplane can be dropped in whole -- and it will then work
in any aircraft, which is the point. A folder named "default" is the same as
putting the files loose in Announcements.

If you already have a pack somewhere else, do not copy it: put its path into
Settings > Immersion > Where they are. Pointing at the pack, at the folder
above it, or at a whole community folder full of packs all work.

The moments, and the file name for each
---------------------------------------

  BoardingWelcome           boarding has begun
  BoardingMusic             under the boarding, after the welcome
  CaptainWelcome            the captain, while the aeroplane is pushed back
  BoardingComplete          everybody is aboard
  ArmDoors                  doors to automatic and cross check
  PreSafetyBriefing         the call before the demonstration
  SafetyBriefing            the safety demonstration
  CabinDimTakeoff           lights down for a night departure
  CrewSeatsTakeoff          cabin crew, take your seats for departure
  CallCabinSecureTakeoff    and the answer back
  AfterTakeoff              the climb, seatbelt sign on
  FastenSeatbelt            turbulence
  DescentSeatbelts          the descent has begun
  CrewSeatsLanding          cabin crew, take your seats for landing
  CallCabinSecureLanding    and the answer back
  AfterLanding              welcome to wherever you have arrived
  DisarmDoors               doors to manual and cross check
  DisembarkStarted          goodbye

Anything with no file falls back to the synthesised crew, so a partial pack
is fine. A moment that has neither is simply silent.

Two of them are not Fenix's. CaptainWelcome is a moment this program has and
that aeroplane does not, because there the captain is the person flying it.
BoardingMusic is in every pack, but where you have no file for it WilcoATC
now plays a quiet bed of its own instead of nothing; your file still wins.

Choosing between several files
------------------------------

Tags in square brackets narrow when a file is used. A file is only considered
if every tag on it applies, and the most specific one wins.

  SafetyBriefing[1].ogg        one of several, picked once per flight and
  SafetyBriefing[2].ogg        then kept for the rest of it
  BoardingWelcome[Morning].ogg 06:00-12:00; also Night, Afternoon, Evening
  BoardingWelcome[EJU].ogg     that operator
  AfterTakeoff[A319].ogg       that aircraft type
  BoardingWelcome[EJU][Evening].ogg     tags combine

Formats
-------

Ogg Vorbis (.ogg) is what the packs use and what Fenix requires. WAV, FLAC,
Opus and MP3 are also read here.
