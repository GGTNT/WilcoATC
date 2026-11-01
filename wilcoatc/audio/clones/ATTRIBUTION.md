# The cloned controllers' reference clips

The sixteen clips in this folder are what WilcoATC-HQ's cloned controllers
are made from. Each is ten to fifteen seconds of one speaker reading, cut
from the CSTR VCTK Corpus and joined, where it took two recordings, with
0.35 s of silence between them. Nothing else was done to them: resampled to
24 kHz, trimmed, and normalised in level.

**Source:** Junichi Yamagishi, Christophe Veaux, Kirsten MacDonald. *CSTR
VCTK Corpus: English Multi-speaker Corpus for CSTR Voice Cloning Toolkit*
(version 0.92). University of Edinburgh, The Centre for Speech Technology
Research (CSTR), 2019. https://doi.org/10.7488/ds/2645

**Licence:** Creative Commons Attribution 4.0 International (CC BY 4.0),
https://creativecommons.org/licenses/by/4.0/. The clips are excerpts of the
corpus's recordings (microphone 1, silence-trimmed release) and are
distributed under the same licence.

The file name says who each controller is: accent, sex, then the corpus
speaker. The accent and region are the corpus's own speaker information.

| Clip | Speaker | Accent | Region | Recordings used |
| --- | --- | --- | --- | --- |
| us_m_vctk_p311 | p311 | American | Iowa | p311_008, p311_003 |
| us_m_vctk_p334 | p334 | American | Chicago | p334_023, p334_021 |
| us_m_vctk_p345 | p345 | American | Florida | p345_023 |
| us_m_vctk_p360 | p360 | American | New Jersey | p360_021, p360_008 |
| us_f_vctk_p294 | p294 | American | San Francisco | p294_023 |
| us_f_vctk_p299 | p299 | American | California | p299_023, p299_021 |
| us_f_vctk_p333 | p333 | American | Indiana | p333_023, p333_021 |
| us_f_vctk_p341 | p341 | American | Ohio | p341_023 |
| gb_m_vctk_p226 | p226 | English | Surrey | p226_008, p226_003 |
| gb_m_vctk_p232 | p232 | English | Southern England | p232_003, p232_008 |
| gb_m_vctk_p258 | p258 | English | Southern England | p258_023, p258_021 |
| gb_m_vctk_p270 | p270 | English | Yorkshire | p270_023 |
| gb_f_vctk_p228 | p228 | English | Southern England | p228_023 |
| gb_f_vctk_p229 | p229 | English | Southern England | p229_008, p229_003 |
| gb_f_vctk_p240 | p240 | English | Southern England | p240_023, p240_021 |
| gb_f_vctk_p276 | p276 | English | Oxford | p276_023, p276_021 |

`scripts/cut_vctk_clones.py` is how they were made, and makes them again.

## The cabin crew's clips

`crew/` holds two more, cut the same way from the same corpus and under the
same licence. They are kept out of the folder above on purpose: a clip there
is copied into the controllers' voice folder and joins their casting, and
these are for `scripts/cabin_announcement.py`. Both are northern English men,
chosen over p270 because his recording has vocal fry in about one frame in
ten, which a clone reproduces as a tremble (measured with pYIN: 9% of his
voiced frames more than 30% under his own median, against 0-2% for these).

| Clip | Speaker | Accent | Region | Recordings used |
| --- | --- | --- | --- | --- |
| crew/gb_m_vctk_p286 | p286 | English | Newcastle | p286_003, p286_005 |
| crew/gb_m_vctk_p278 | p278 | English | Cheshire | p278_023, p278_021 |
