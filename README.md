# LiveDub — Real-time translation and dubbing (Windows + macOS)

**English** · [Türkçe](README.tr.md)

LiveDub **translates and voices** whatever your computer is playing (YouTube, films, streams, Zoom…)
or your own microphone, **while the speaker is still talking**. The translation is streamed as it is
produced and played to the output you pick: speakers, headphones, or a virtual cable that goes into
your meeting app.

> The app's interface is currently in Turkish. Labels below are quoted as they appear in the app,
> with an English gloss.

## Engines

| Engine | How it works | Latency | Output languages | Keys needed |
|---|---|---|---|---|
| **Gemini Live Translate** (`gemini-3.5-live-translate-preview`) — default | Simultaneous translation while the speaker talks; adapts the voice to the speaker and doesn't repeat speech that is already in the target language | Lowest (flows with the speech) | 70+ languages, **including Turkish** | Gemini ([get one free](https://aistudio.google.com/apikey)) |
| **OpenAI Realtime Translate** (`gpt-realtime-translate`) | Simultaneous translation while the speaker talks; adapts the voice to the speaker | Lowest (flows with the speech) | en, es, pt, fr, ja, ru, zh, de, ko, hi, id, vi, it — **no Turkish** | OpenAI |
| **OpenAI Realtime GPT** (`gpt-realtime-1.5` etc.) | A speech model prompted as an interpreter; translates at every pause | Low (after a pause) | All languages, **including Turkish** | OpenAI |
| **Cascade** | Deepgram live STT → OpenAI/DeepL translation → OpenAI/ElevenLabs TTS | Medium (after the end of a sentence) | All languages, any voice you like (ElevenLabs) | Deepgram + OpenAI/DeepL + OpenAI/ElevenLabs |

In short: in both directions (into and out of Turkish) the default engine is **Gemini Live
Translate** — simultaneous and the cheapest. On the free tier audio input is free and translated audio
costs about $0.018/min, roughly $1 an hour. On the free tier, content you send may be used by Google to
improve its products. For comparison, OpenAI Realtime Translate is $0.034/min. If you want a custom or
cloned voice, pick *Cascade*.

Prices are from the official pricing pages as of September 2026.

What keeps latency down:
- Audio is captured in 20 ms blocks and streamed over WebSocket in 40 ms packets.
- In Realtime GPT the dub is **not cut off** when new speech starts; responses are queued. In continuous
  speech (videos, streams) a translation is triggered at the latest after *Longest chunk* seconds
  (7 s by default).
- Old conversation items are deleted, so cost and latency don't grow as the session gets longer.
- In Cascade the translation streams token by token and TTS starts as soon as the first sentence is done.
  If the queue builds up, playback speeds up automatically.
- If the connection drops (or the server session expires), it reconnects automatically.
- The status bar shows live **Latency** (end/start of speech → first dubbed audio) and **Queue**
  (dubbed audio waiting to be played).

## Install

LiveDub is a regular desktop app; you don't need Python to use it.

There is no tagged release yet. To get a build, run the **build** workflow on GitHub
(*Actions → build → Run workflow*, see [.github/workflows/build.yml](.github/workflows/build.yml)) and
download the Windows and macOS zips from the run's *Artifacts*.

- **Windows:** unzip and run `LiveDub/LiveDub.exe`. You can move the folder anywhere and create a desktop
  shortcut to `LiveDub.exe`. The `_internal` files in the folder are part of the app.
- **macOS:** drag `LiveDub.app` into *Applications*. It asks for microphone permission on first launch.
  The build is unsigned, so the first time right-click → *Open*. The Mac build is for Apple Silicon.

To build it yourself:
```bash
pip install -r requirements-build.txt
pyinstaller livedub.spec --noconfirm
```
Each platform builds on itself: an exe on Windows, an .app on a Mac. To distribute the Mac build to
others you need to sign and notarize it with an Apple developer account.

### Developer mode (from source)

To skip rebuilding while you work on the code, `run_windows.bat` / `run_macos.command` set up a
Python 3.10+ virtual environment and start the app from source. You don't need them for normal use.
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip ...
.venv/bin/python -m livedub
```

Enter your key under **Ayarlar → API anahtarları** (*Settings → API keys*), or set environment variables
such as `GEMINI_API_KEY` and `OPENAI_API_KEY`. The **Ses testi** (*Audio test*) button needs no API: it
plays a beep on the output and shows the input level. Use it first to check your device setup.

## Use cases

### 1) Dubbing videos and streams on Windows

Source = `Sistem sesi: tüm uygulamalar, dublaj hariç` (*System audio: all apps, excluding the dub*),
Output = your headphones or speakers. Nothing to install. LiveDub captures everything that plays but
leaves out its own dub (Windows 10 build 20348+ / Windows 11).

In this mode the **Orijinal ses** (*Original audio*) slider sets the volume of the other apps:
- **0%:** you only hear the dub. Other apps are turned down to 1%, and LiveDub boosts what it captures
  by the same amount, so the translation isn't affected. Windows hands over app audio after the volume
  is applied, so apps can't be fully muted: muting them would also silence what needs translating.
- **100%:** you hear the original and the dub together.
- When you stop, volumes go back to how they were. Even if the app crashes, the old levels are restored
  on the next launch.

If the dub leaks back into the captured audio, the model translates its own voice and repeats the same
sentences in a loop. That's why, when a `Sistem sesi: <device>` source is the same device as the
output, LiveDub switches to "excluding the dub" capture automatically.

**Alternative, with a virtual cable (without touching app volumes):**
1. Install the free [VB-CABLE](https://vb-audio.com/Cable/).
2. Under *Settings → System → Sound → Volume mixer / app volume and device preferences*, set your
   browser's or player's output to **CABLE Input**.
3. In LiveDub: Source = `Sanal kablo: CABLE Output` (*Virtual cable*), Output = your speakers.
4. To hear the original quietly in the background, raise **Orijinal ses**; it ducks automatically
   while the dub is speaking.

### 2) Dubbing system audio on macOS

macOS doesn't let apps capture system audio directly; you need a free virtual cable:
1. Install [BlackHole 2ch](https://existential.audio/blackhole/) (`brew install blackhole-2ch`) and restart the Mac.
2. In **System Settings › Sound › Output**, select **BlackHole 2ch**. This step is required: installing it
   isn't enough, and if system audio doesn't go to the cable LiveDub receives nothing.
3. In LiveDub: Source = `Sanal kablo: BlackHole 2ch`, Output = **MacBook speakers / headphones**. If you
   leave the output on "Default" and the default is BlackHole, LiveDub routes the dub to the real
   speakers or headphones automatically. Don't pick a Multi-Output device; the dub would go back into
   BlackHole.
4. **Orijinal ses** slider: 0% = dub only; raise it to hear the original too.
5. On first start macOS asks for microphone permission. BlackHole counts as a "microphone", so you need
   to allow it. If you denied it: turn on **System Settings › Privacy & Security › Microphone › LiveDub**
   and restart the app.

When you're done, switch the Mac's output back to your speakers or headphones. If no audio arrives for a
few seconds, LiveDub says why in the interface (permission off, nothing reaching the cable, etc.).
In Terminal, `/Applications/LiveDub.app/Contents/MacOS/LiveDub --diagnose` shows the permission state
and devices.

### 3) Microphone → your translated voice in a meeting (Zoom, Meet, Discord, Teams)

1. Source = your microphone, target language = the other side's language.
2. Output = **CABLE Input** (Windows) or **BlackHole 2ch** (macOS).
3. In the meeting app, choose **CABLE Output** / **BlackHole 2ch** as the microphone.

To speak Turkish and be heard in English, *Gemini Live Translate* is again the fastest and cheapest engine.

### 4) Microphone → to the person next to you, through the speaker

Source = microphone, Output = speaker. Since the microphone will hear the speaker, feedback protection
kicks in: you speak, the dub plays, then you speak again (turn-taking).

## Settings

- **Noise reduction:** `near_field`/`far_field` for a microphone; leave it off for system audio.
- **Silence threshold (Realtime GPT):** lowering it (e.g. 250 ms) reduces latency but can split sentences.
- **Speech speed:** Turkish translations tend to be longer than the source; 1.1–1.2x keeps the dub from
  falling behind.
- **Cascade + Deepgram:** automatic language detection works with `nova-3`; if you pick a specific source
  language, choose a model that supports it (`nova-2` is the safest for Turkish source audio).
- Model names are editable fields, so you can use a new OpenAI model as soon as it ships, without
  changing code.

Settings and keys are stored **in plain text** in `~/.livedub/settings.json`.

## Terminal mode

```bash
python -m livedub --list-devices
python -m livedub --test-audio --source "lb:{...}" --output "out:Speakers (Realtek(R) Audio)"
python -m livedub --headless --engine realtime --target tr --source "in:CABLE Output (VB-Audio Virtual Cable)"
```

## Tests

```bash
python tests/mock_engines.py
```
Runs the engines against local fake servers (no API keys or audio devices needed).

## Project layout

```
livedub/
  audio/devices.py     device listing, feedback-risk detection
  audio/capture.py     microphone/virtual cable (sounddevice) + Windows WASAPI loopback (soundcard)
  audio/player.py      low-latency output, mixing and ducking the original audio
  engines/             gemini_translate.py, openai_translate.py, openai_realtime.py, cascade.py
  session.py           capture → engine → playback pipeline (background thread + asyncio)
  gui.py               PySide6 interface
  cli.py               terminal mode
```

## Troubleshooting

- **No audio at all:** use *Ses testi* to check that the input meter moves. On Windows, loopback doesn't
  work while apps use *exclusive mode*.
- **The dub repeats the same sentences in a loop:** the dub is leaking back into the captured audio. On
  Windows, choose `Sistem sesi: tüm uygulamalar, dublaj hariç` as the source. On macOS, make sure the
  output isn't going to BlackHole.
- **OpenAI Realtime Translate won't speak Turkish:** the model doesn't support Turkish output; pick
  *Gemini Live Translate*.
- **Gemini "quota" error:** you've hit the free tier's limit. Wait a while or enable billing in Google AI
  Studio.
- **"API key invalid":** check the key and that your account has Realtime API access.

---

Built by [Yusuf Kağan Kılıç](https://yusufkaanklc.dev/en/).
