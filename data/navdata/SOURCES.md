# Where the navigation data comes from

Every file in this folder that ships with WilcoATC, its source and its
licence. The program is free and open source: nothing here is under a
non-commercial licence except where this file says so, and that one item is
never bundled.

| File | What it holds | Source | Licence |
|---|---|---|---|
| `nav.sqlite` | Airports, runways, frequencies, navaids | Built by `setup` from [OurAirports](https://ourairports.com/data/) | Public domain |
| `Boundaries.geojson`, `VATSpy.dat` | FIR and sector outlines | VATSpy data project | CC BY-SA 4.0 |
| `approach_areas.json.gz` | The approach units' outlines, used as geometry only (where departure hands a flight to the centre, and the centre to approach); 1,149 approach and departure areas of the 1,355 in the source (tower, centre and information zones left out), simplified to about a third of a nautical mile (161 kB) | [SimAware TRACON Project](https://github.com/vatsimnetwork/simaware-tracon-project), `TRACONBoundaries.geojson` v1.2.13, by the SimAware TRACON Project contributors, built by `scripts/build_approach_areas.py` | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); this file is an adaptation of it and is under the same licence. Simulated airspace for flight simulation, not kept to the AIRAC cycle, not for real-world navigation |
| `circuits_faa.csv` | Circuit side per US runway end, traffic pattern altitude per US field | FAA NASR 28-day subscription (APT_RWY_END, APT_BASE), built by `scripts/build_nasr.py` | US federal government work, public domain (17 U.S.C. 105) |
| `vrps_faa.csv` | US charted VFR waypoints (VPxxx), each attached to the nearest towered field within 25 NM | FAA NASR (FIX_BASE, use code VFR), built by `scripts/build_nasr.py` | Public domain (17 U.S.C. 105) |
| `airspace_faa.csv` | US class B, C, D and surface E (E2, E3, E4) volumes with their limits, outlines simplified to about 30 m | FAA NASR class airspace shape files, built by `scripts/build_nasr.py` | Public domain (17 U.S.C. 105) |
| `sectors_faa.csv` | The US ARTCCs as en-route sectors: each centre's boundary below FL180 and above, its callsign, and every VHF frequency of its remote sites serving that stratum (21 ARTCCs, 42 rows, 50 kB, cycle 2026-10-01) | FAA NASR (ARB_SEG, FRQ RCAG rows), built by `scripts/build_nasr.py` | Public domain (17 U.S.C. 105) |
| `centre_sites_faa.csv` | Each ARTCC remote site's VHF frequencies with the stratum they serve and where the site is, so the frequency given is the nearest one (814 rows, 76 kB) | FAA NASR (FRQ, RCAG rows), built by `scripts/build_nasr.py` | Public domain (17 U.S.C. 105) |
| `circuits.csv` | Circuit side and height, kept by hand | Each row names its AIP page and date | The facts of a state's AIP, typed by hand |
| `vrps.csv` | Visual reporting points, kept by hand | Each row names its chart and date (France: SIA VAC charts) | As above |
| `airspace.csv` | Airspace volumes, kept by hand | Each row names its AIP section and date | As above |
| `service_hours.csv` | When a tower or information service is staffed, kept by hand | Each row names its AIP section and AIRAC cycle | As above |
| `preferred_runways.csv`, `minimum_altitudes.csv`, `overrides.csv` | Local corrections, kept by hand | Each row names its source | As above |
| `fir_frequencies.csv` | Area control frequencies per FIR, with levels where a state splits them, and HF families for oceanic FIRs | Typed by hand from each state's AIP (ENR 2.1); each row names the document, the URL it was read at and the AIRAC date | The facts of a state's AIP, typed by hand |
| `sectors.csv` | The pilot's own en-route sectors, ahead of every other source. Shipped empty | Whoever adds a row names its source | Yours |
| `taxiway_words.csv` | Taxiways whose names are words, and how each is said (Brussels' OUT is "Outer"), per field; the rest fall back to an automatic rule | Each row names its source | The facts of a scenery and a state's AIP, typed by hand |

## The one non-commercial dependency

The en-route sectors (who works a flight at which level) can come from the
[VatGlasses data](https://github.com/lennycolton/vatglasses-data), by the
VatGlasses contributors, under **CC BY-NC-SA 4.0**: attribution,
non-commercial, share-alike. It is not part of WilcoATC. It is downloaded
only when the pilot asks for it, after being shown that licence, into its own
`vatglasses/` folder, and never bundled with a build.

## Rebuilding the FAA files

Every 28 days the FAA publishes a new cycle. Download, for one cycle, the
APT, FIX and CLS_ARSP CSV groups and `class_airspace_shape_files.zip` from
the NASR subscription page, unzip them into one folder, and run:

    python scripts/build_nasr.py <that folder> <cycle date, e.g. 2026-10-01>

The cycle in use is written at the top of each generated file.

## Rebuilding the approach areas

Download `TRACONBoundaries.geojson` from the SimAware TRACON Project's
latest release and run:

    python scripts/build_approach_areas.py TRACONBoundaries.geojson <release tag>

The release tag is written into the file and shown by `freq --audit`.

## Live traffic: asked at runtime, never shipped

Nothing in this folder comes from these. They are asked by the program in
flight, only with `traffic.live.enabled` on, and only the pilot's position
and a radius are sent (`integrations/adsb.py`).

| Service | What it gives | Terms, as its own pages say them (read 30 Oct 2026) |
|---|---|---|
| [adsb.lol](https://adsb.lol) | Every ADS-B aircraft within 250 nm of a point, readsb JSON | No key today; "Rate limits are dynamic based on the environment load"; a key "in the future". Answering 401/403, it is dropped for the session and no key is ever asked of the pilot |
| [adsb.fi](https://adsb.fi) open data | The same, in the same shape | "for personal, non-commercial use only"; public endpoints "rate limited to 1 request per second"; credit adsb.fi with a link to its home page (done on the traffic card and in the README) |
| [adsbdb.com](https://www.adsbdb.com) | A callsign's route, an address's type and registration | No published rate limit; the route data "may not be copied, published, or incorporated into other databases without the explicit permission of David J Taylor, Edinburgh". Answers are kept on the pilot's own machine (`data/adsb_cache.json`, a day at most), never shipped; the test fixture of a route is invented in its shape |

The recorded answers in `tests/data/adsb/` are test inputs only; see the
README there.
