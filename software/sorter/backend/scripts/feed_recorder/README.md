# Feed recorder

Records the sorter's camera feeds from another machine on the same network,
for hours, without loading the sorter: the backend re-serves each camera's own
JPEG bytes (no decode or encode on the sorter, nothing extra on its USB bus)
and this script does the decoding and encoding where the CPU and GPU are.

What it writes, per role, in a dated session folder under `--out`:

| File | What |
|---|---|
| `raw_NNN.mp4` | full resolution, low bitrate, paced by wall clock (gaps freeze rather than skip) |
| `annotated_NNN.mp4` | the same frames with the live-feed overlay (zones, boxes, track ids) composited |
| `stills/*.jpg` | the camera's own JPEG every `--still-every` seconds, untouched |
| `events.ndjson` | every inference event the backend produced |
| `index.csv` | video frame number to source frame timestamp and sequence |

Backend endpoints it uses (all read-only):

- `GET /api/recording/status`
- `GET /api/recording/raw/{role}` multipart MJPEG with `X-Timestamp` / `X-Seq` per part
- `GET /api/recording/events/{role}` newline-delimited JSON
- `GET /api/recording/zones/{role}` static overlay arrays as PNGs

Requirements on the recording host: `python3` with `cv2` and `numpy`, and
`ffmpeg`. NVENC (`hevc_nvenc`) is the default encoder; pick `--encoder
libx264` on a machine without an NVIDIA GPU. The script falls back to libx264
by itself if the chosen encoder fails to start.

Example, twelve hours at 5 fps with a full-quality still every 20 s:

```bash
python3 record.py --backend http://<sorter-ip>:8000 --out /path/to/recordings \
    --roles carousel,c_channel_2,c_channel_3 --hours 12 --fps 5 --still-every 20
```

Run it under `nohup` or a systemd unit; it logs to stdout and to
`recorder.log` in the session folder, reports sizes every five minutes, and
writes a per-role summary into `session.json` when it finishes. `SIGTERM`
stops it cleanly. Streams reconnect on their own when the sorter's cameras or
network drop, so it survives a camera hub reset mid-run.

Sizes at the defaults (5 fps, HEVC, 400/600 kbit/s for the large feed and
120/200 kbit/s for 720p feeds, raw plus annotated): about 0.75 GB per hour of
video for three feeds, so roughly 9 GB for twelve hours, plus stills at the
camera's JPEG size (a 1440p still is about 0.5 MB, a 720p one about 0.15 MB).

Videos are written in numbered segments: a new segment starts whenever the
camera comes back at a different frame size (a USB hub reset can do that), so
no file is ever written with mixed sizes. `--max-gb` stops the run when the
session folder exceeds a size. Zones and the events stream are fetched with
retries, so the recorder can be started before the sorter is up and will pick
up the overlay once perception is running.

To run it as a service, a systemd unit with `Restart=on-failure` and
`RequiresMountsFor=` on the output disk keeps it going across reboots of the
recording host; the sorter rebooting needs nothing, the streams reconnect.
