# Deploy notes

## Orange Pi 4 Pro (`192.168.1.6`, aarch64)

The Qwen transport drives a headed browser, so the unit needs a display, a
browser binary and `patchright` installed in the *same* virtualenv the service
runs from. All three are easy to get wrong, and each failure looks like a
different problem:

| Symptom | Cause |
|---|---|
| `patchright is not installed` | the service runs the system `python3`, not `.venv/bin/python` |
| `Executable doesn't exist at .../chromium-1248/...` | patchright wants its own revision; point it at the bundled one |
| `Target page, context or browser has been closed` | no `DISPLAY`, so the headed browser cannot open |
| the page comes up logged out | the cookie was set on `chat.qwen.ai` instead of `.qwen.ai` |

### One-time setup

```bash
ssh orangepi@192.168.1.6
cd ~/danyapi3
.venv/bin/pip install patchright
# The board already has a Playwright Chromium from the Pinterest project:
ls ~/.cache/ms-playwright/          # e.g. chromium-1194
```

### Unit configuration

`deploy/systemd/danyapi3.service.d/override.conf` carries the settings:

```ini
Environment=DISPLAY=:0
Environment=QWEN_BROWSER_EXECUTABLE=/home/orangepi/.cache/ms-playwright/chromium-1194/chrome-linux/chrome
Environment=HOME=/home/orangepi
Environment=DANYAPI_QWEN_CAPTCHA_WAIT=0
```

Check the revision in the path against `~/.cache/ms-playwright/` before
installing — a wrong path fails with "Executable doesn't exist".

```bash
sudo mkdir -p /etc/systemd/system/danyapi3.service.d
sudo cp deploy/systemd/danyapi3.service.d/override.conf /etc/systemd/system/danyapi3.service.d/
sudo systemctl daemon-reload
sudo systemctl restart danyapi3
```

### Verify

```bash
systemctl is-active danyapi3
curl -s http://127.0.0.1:6660/health | python3 -m json.tool | head -20
```

`qwen_stats` should show both accounts healthy. Then one image request — the
first one opens the browser, so allow up to two minutes:

```bash
curl -s --max-time 250 http://127.0.0.1:6660/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-image-3.0-pro","prompt":"a red apple","size":"1024x1536","n":1}' \
  | head -c 200
```

### Things to know

- **A captcha needs a human.** With `DANYAPI_QWEN_CAPTCHA_WAIT=0` the request
  fails immediately and `/health` reports `broken_captcha` with
  `captcha_resets_in`. To clear it, run the transport with the display forwarded
  over VNC, or set a wait budget and solve the slider in the open window.
- **One profile per account** lives under `~/.danyapi/qwen-browser-<n>`. Deleting
  a profile logs that account out of the browser.
- **Do not use `pkill -f 'python -m danyapi'`** — the pattern matches both
  instances. Stop one by its port: `fuser -k -n tcp 6660`, or `systemctl stop
  danyapi3`.
