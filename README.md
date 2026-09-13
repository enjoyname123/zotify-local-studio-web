# Zotify Local Studio

Zotify Local Studio is a private, local web GUI for [Zotify](https://github.com/zotify-dev/zotify). It runs Zotify sequentially, streams its terminal output live, and persists downloader preferences in `config/config.json`.

## Docker Compose (recommended)

1. Install Docker Desktop (Windows/macOS) or Docker Engine and Compose (Linux).
2. From this directory, create the persistent folders:

   ```sh
   mkdir music config
   ```

3. Start the application:

   ```sh
   docker compose up --build -d
   ```

4. Open http://localhost:8000. Upload a file named `credentials.json` in the Credentials panel, or place it in `config/credentials.json`.
5. Stop it with `docker compose down`. Downloads remain in `music/`, and settings/credentials remain in `config/`.

## Bare metal: Windows PowerShell

Install Python 3.11+, Git, and FFmpeg (ensure `ffmpeg.exe` is on `PATH`). Then:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
pip install git+https://zotify.xyz/zotify/zotify.git
New-Item -ItemType Directory -Force music, config
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Visit http://127.0.0.1:8000.

## Bare metal: Linux/macOS

Install Python 3.11+, Git, and FFmpeg using your system package manager (`brew install ffmpeg` on macOS or `sudo apt install ffmpeg git` on Debian/Ubuntu), then:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
pip install git+https://zotify.xyz/zotify/zotify.git
mkdir -p music config
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Keep the terminal running while downloading.

## Android via Termux

Install Termux from F-Droid, then run:

```sh
pkg update
pkg install python ffmpeg git
git clone <your-copy-of-this-project>
cd Flask-Audio-Downloader
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
pip install git+https://zotify.xyz/zotify/zotify.git
mkdir -p music config
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Visit `http://127.0.0.1:8000` in the Android browser. Termux storage can be exposed with `termux-setup-storage`; choose a writable output path in the UI if needed.

## Notes

- Spotify credentials are required by Zotify and are never sent to a third-party service by this app.
- The web server is intentionally local by default. If binding to `0.0.0.0`, protect port 8000 with a firewall or private network.
- Queue items use the settings present when they are added, so changing preferences does not alter an already queued download.
- The backend discovers the `zotify` executable on `PATH` and falls back to `python -m zotify`, supporting virtual environments and common platforms.
