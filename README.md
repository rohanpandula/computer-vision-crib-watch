# computer-vision-crib-watch

Log your baby's sleep to Huckleberry from a crib camera, using a vision model that runs on your own hardware.

Every two minutes the script takes a snapshot of the crib camera through Home Assistant and asks a local [Ollama](https://ollama.com) vision model one question: is the crib empty, or is the baby asleep or awake? It then starts and stops the Huckleberry sleep timer through the [Huckleberry integration for Home Assistant](https://github.com/Woyken/huckleberry-homeassistant). Camera frames stay on your network.

> [!WARNING]
> This is a sleep log, not a baby monitor. It can be wrong and it can go offline. Do not rely on it for your child's safety.

## Features

- Works with any camera Home Assistant can take a snapshot from, including night vision
- Sleeps are backdated to the first frame the baby was seen asleep
- Short stirs do not split a sleep, and very short sleeps are thrown away
- Can use the camera's motion sensor to look right away when the baby stirs, so wake times are accurate to seconds
- Retries a failed Huckleberry call until it goes through, and can send a push when the camera or model has been down for a while
- Leaves the timer alone if someone already started one by hand
- Dry-run mode records what it would log without touching Huckleberry
- A one-tap review page for checking the model's reads on your phone
- Problem alerts through Home Assistant, ntfy, Pushover, Pushbullet, Telegram, Gotify, Discord, Slack or a webhook
- Two small Python files, standard library only, no image to build
- Optional cry alerts for cameras that have a microphone but no cry detection of their own

## Requirements

- Home Assistant with a camera entity pointed at the crib
- The Huckleberry integration set up for your child
- An Ollama server with a vision-capable model. It was built and tested with `gemma4:12b-it-qat` on a 16 GB GPU.
- Docker, or Python 3.10 or newer

## Getting started

1. Clone the repository and create your settings file.

   ```sh
   git clone https://github.com/rohanpandula/computer-vision-crib-watch.git
   cd computer-vision-crib-watch
   cp .env.example .env
   chmod 600 .env
   ```

2. Fill in `.env`.

   | Setting | What it is |
   |---|---|
   | `HA_URL` | Address of your Home Assistant |
   | `HA_TOKEN` | A long-lived access token, created under your Home Assistant profile, Security tab |
   | `CAMERA` | The crib camera entity, for example `camera.crib` |
   | `HUCKLEBERRY_DEVICE_ID` | The child's device id (see below) |
   | `SLEEP_SENSOR` | The child's sleep sensor from the Huckleberry integration |
   | `OLLAMA_URL`, `MODEL` | Your Ollama server and vision model |
   | `DRY_RUN` | `1` records only, `0` writes to Huckleberry |
   | `NOTIFY_SERVICE` | Optional. A Home Assistant notify service for problem alerts, for example `notify.mobile_app_my_phone`. Other platforms are listed under [Problem alerts](#problem-alerts) |
   | `CRY_SENSOR` | Optional. A cry sensor, for example `binary_sensor.crib_cry` from the cry alerts below |
   | `MOTION_SENSOR` | Optional. The camera's motion sensor, for example `binary_sensor.crib_motion` |

   To find the device id, open Developer tools, then Template, in Home Assistant and enter:

   ```jinja
   {{ device_id('sensor.baby_sleep') }}
   ```

3. Start it and watch the first few reads.

   ```sh
   docker compose up -d
   docker logs -f sleep-logger
   ```

> [!TIP]
> Leave `DRY_RUN=1` for the first nap or night. Compare `data/events.jsonl` with what really happened, then set `DRY_RUN=0` and run `docker compose up -d` again.

## How it decides

| Rule | Default | Setting |
|---|---|---|
| Asleep reads in a row before a sleep starts | 3 | `ASLEEP_CHECKS` |
| Time awake or out of the crib before a sleep ends | 10 minutes | `WAKE_MIN` |
| Sleeps shorter than this are cancelled instead of saved | 10 minutes | `MIN_SLEEP_MIN` |
| Camera or model unreachable for this long ends the sleep | 60 minutes | `OFFLINE_END_MIN` |
| Time between checks | 120 seconds | `INTERVAL_S` |

A sleep ends at the first frame where the baby was awake or gone, not at the moment the rule fires. The frame that confirms it must still show the baby awake or gone. If the camera drops out, the sleep ends at the last frame where the baby was seen asleep.

If you set `MOTION_SENSOR`, new motion during a sleep triggers a check right away instead of waiting for the next one. Motion never ends a sleep by itself. It only makes the script look sooner, and the vision model still decides.

If a call to Huckleberry fails, it is kept and tried again on every check until it goes through, including across restarts.

If you set `CRY_SENSOR`, a cry during a sleep also triggers a check right away. If the baby is then seen awake or gone within 10 minutes, the sleep ends at the time of the cry, since that is when the baby really woke. A cry the baby settles from changes nothing.

When the model is unsure, or a snapshot fails, that check is ignored. It neither starts nor ends anything.

## Reviewing its calls

Everything it writes goes to the `data` folder:

- `events.jsonl` has one line per sleep it started, completed or cancelled
- `frames/` keeps the snapshots worth a second look for three days: each change of state, each look a cry asked for, and awake reads and motion looks at most once every 10 minutes
- `labeled/` holds the frames you have reviewed (see below). These are kept until you delete them
- `state.json` lets a sleep in progress survive a restart

> [!NOTE]
> The saved frames are pictures of your child. They never leave the machine running the script, but treat that folder accordingly.

### Review page

The review page shows those saved frames one at a time with the model's read, and you tap what the frame really shows: asleep, awake or empty. Awake reads and cry-triggered looks come first. It is built for a phone, and there is an undo for a wrong tap.

```sh
docker compose --profile review up -d
```

Then open `http://<the machine running it>:8765`. Change the port with `REVIEW_PORT`.

Reviewing changes nothing in Huckleberry. Each reviewed frame moves to `data/labeled/` with your answer in its file name, for example `20260101-031500_asleep_is_awake.jpg`, and the page shows how often the model has been right. That folder is a ready-made test set for changing the prompt or trying another model.

> [!WARNING]
> The review page has no login. Anyone who can reach that port on your network can see the frames. Do not expose it to the internet.

## Problem alerts

The script can tell you when it needs help: the camera or the model has been unreachable for 15 minutes (`BLIND_ALERT_MIN`), or 15 Huckleberry calls in a row have failed (`RETRY_ALERT_TRIES`), repeated every 15 more while it lasts. Fill in the settings for any of these platforms in `.env` and every one that is filled in gets the message.

| Platform | Settings |
|---|---|
| Home Assistant | `NOTIFY_SERVICE` |
| ntfy | `NTFY_TOPIC`, optionally `NTFY_URL` and `NTFY_TOKEN` |
| Pushover | `PUSHOVER_TOKEN`, `PUSHOVER_USER` |
| Pushbullet | `PUSHBULLET_TOKEN` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Gotify | `GOTIFY_URL`, `GOTIFY_TOKEN` |
| Discord | `DISCORD_WEBHOOK` |
| Slack | `SLACK_WEBHOOK` |
| Anything else | `WEBHOOK_URL`, which receives JSON with `title` and `message` |

> [!NOTE]
> Only the Home Assistant platform has been used for real. The others follow each service's published API but are untested. An alert sent through Home Assistant cannot tell you that Home Assistant itself is down, which is one reason to add a second platform.

These are alerts about the script, not about the baby. The cry notification below is a Home Assistant automation, and it can use any notify service Home Assistant supports.

## Cry alerts (optional)

Some cameras have a microphone but no baby-cry detection. The `cry-alert` folder adds it: a [Frigate](https://frigate.video) container listens to the camera's audio, and Home Assistant sends a notification to your phone when it hears crying.

This part is separate from the sleep logger. It needs no vision model and no GPU, and either one works without the other. Frigate is used only as a sound classifier here, with video detection and recording turned off.

You need an MQTT broker connected to Home Assistant, and an RTSP stream from the camera that carries audio.

1. Create the settings file and fill in the MQTT login and the camera's RTSP address.

   ```sh
   cd cry-alert
   cp .env.example .env
   chmod 600 .env
   ```

2. If your MQTT broker is not at `homeassistant.local`, change `mqtt.host` in `config/config.yml`. Then start the container.

   ```sh
   docker compose up -d
   ```

3. Copy `home-assistant/crib_cry.yaml` into your Home Assistant `packages` folder, change the phone and camera entities named at the top of the file, and restart Home Assistant.

To check the audio is arriving, look at the `Crib Audio Level` sensor in Home Assistant. A quiet room reads around -60 dBFS and the number rises with noise.

Two settings in `config/config.yml` control how easily it alerts:

| Setting | Default here | What it does |
|---|---|---|
| `min_volume` | 300 | How loud the room must be before the classifier runs. Lower is more sensitive. |
| `threshold` | 0.7 | How confident the classifier must be that the sound is crying. Lower gives more alerts and more false ones. |

You get at most one cry notification every two minutes, and a separate notification if the audio feed has been gone for five minutes.

By default the cry notification is an ordinary one, so it follows your phone's silent and Focus settings. The package also adds a switch to Home Assistant called `Crib cry critical alert`. Turn it on and the notification becomes an iOS critical alert, which sounds even when the phone is silenced.

> [!NOTE]
> Critical alerts are an iPhone feature and must be allowed for the Home Assistant app in the iOS notification settings. On Android the switch has no effect.

## Tests

The sleep rules are a small state machine with a built-in check:

```sh
python3 sleep_logger.py --selftest
python3 review.py --selftest
```

## Limitations

- Tested so far on an overhead night-vision view. Daytime views, awake frames and empty-crib frames have had much less testing.
- It only sees the crib. Naps in a car seat, stroller or anyone's arms still need logging by hand.
- A baby lying still with their face hidden will probably be read as asleep.
- An empty sleep sack left in the crib was once read as a sleeping baby. The prompt now tells the model to look for a head or face, which fixed the saved example, but other objects may still fool it.
- While it runs, the vision model stays loaded on the Ollama server and holds its memory.
- Cry alerts have been checked with a live microphone feed and a simulated detection, but not yet against a real cry. Expect to adjust `min_volume` and `threshold` for your room.
- The critical alert switch has not been tried on a phone yet.
- The model has not yet been seen to read an awake baby correctly. In practice a cry or an empty crib ends the sleep. The review page is there to collect the frames needed to fix this.
