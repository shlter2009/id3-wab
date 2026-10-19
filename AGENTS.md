# Working on ID3 Sit Web

- Use the existing checkout and its `.venv`; keep this separate from `/workspace/id3-sit`.
- For every user-facing revision, increase `VERSION` in `music_manager/release.py` and add a dated entry to `HISTORY`. The site must visibly show the running version and change history on desktop, mobile, and the metadata dialog. Package/API/template versions derive from that module; do not add separate version literals.
- Keep original uploaded filenames for recognition, separate from short storage/output filenames. Never commit user audio or alter uploaded originals.
- Music matches remain suggestions requiring confirmation. Preserve live/remix/featured identities, distinguish album annotations from titles, and reject unrelated songs with an artist's name as their title.
- Run meaningful core/API tests and `scripts/web_smoke.py` for changed browser flows. Build and run `scripts/container_smoke.py` for packaging changes. Live API checks are separate from mocked tests.
- The existing Render service is `https://id3-sit-web.onrender.com`, using this repository's `main`. Do not create a duplicate service. A Git push does not establish a successful deployment: verify its public version before reporting it as deployed. When access is blocked, report that limit and provide the existing service's deployment steps.
- Never print secrets or request secret values in chat. Preserve network restrictions, TLS verification, existing files and local preferences.
