# Maintained browser-use fork

`254808127/browser-use:main` is the maintained integration branch. All local
browser-use fixes must land here; consumers should pin a tested commit from
this branch. Updating this branch does not update existing deployments or pins.

## Patches to preserve

- Session attach/detach races: register target/session mappings atomically and
  discard detached pending attaches. Regression: `test_session_manager.py`.
- OOPIF lookup: resolve live iframe targets without scanning all target frame
  trees. Regression: `test_frame_collection.py`.
- Downloads: retain browser Blob references and read bounded CDP IO chunks,
  avoiding whole-file numeric arrays returned by value. Finish cancellation
  cleanup before publishing the file. Regressions: `test_downloads_streaming.py`
  and `test_downloads_streaming_browser.py` (real Chromium).

Review these patches when upstream implements an equivalent fix. Do not delete
a patch or its regression tests solely because Git reports a clean merge.

## Operational automation

The upstream sync runner and notification configuration are maintained outside
this source repository on the maintenance host. Do not add scheduling scripts
or notification credentials to this repository.

## Initial integration validation (2026-09-21)

Upstream: `d8110c5ff87ccba887aaa726cdb780f2f84bef8d`.

- 51 patch/download regression tests passed, plus 6 CDP/reconnect tests.
- Pre-commit passed for the retained patch changes.
- Local ACE, 2 GB limit: PDF, CSV, ZIP and XLSX byte/hash checks passed;
  PDF-specific entry point passed; cgroup OOM and OOM-kill counters stayed zero.
- An isolated production Lexmount session downloaded the original S2 MTA PDF
  through both patched entry points: 24,865,690 bytes, matching SHA-256
  `2a036ad51d4c04ba6784a1f9f280c2637f1d9794af154513b920f1bf60b5b07e`.
  The browser remained responsive afterwards and the session was cleaned up.
- Extended local iframe/navigation checks encountered a Page.navigate timeout.
  The same cross-origin test failed at navigation on unmodified upstream in the
  same environment. This is an unresolved integration-test limitation, not a
  passing result or proof that every browser workflow works.

The original S1 OOM cause remains unresolved. The download fix must not be
described as a fix for all OOMs.
