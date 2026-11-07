"""Optional dedicated Firefox audio sink; setup is explicit and reversible."""
from pathlib import Path
import shutil
import subprocess


def pactl(*args):
    executable = shutil.which("pactl")
    if not executable:
        local = Path(__file__).resolve().parent.parent / ".tools/sysroot/usr/bin/pactl"
        if local.exists():
            executable = str(local)
    if not executable:
        raise RuntimeError("Install pulseaudio-utils to use audio routing commands")
    return subprocess.check_output([executable, *args], text=True).strip()


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "list", "move", "remove"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.action == "create":
        sinks = json.loads(pactl("--format=json", "list", "sinks"))
        if any(sink["name"] == "badge_tiktok" for sink in sinks):
            print("badge_tiktok already exists; capture badge_tiktok.monitor")
        else:
            module = pactl("load-module", "module-null-sink", "sink_name=badge_tiktok",
                           "sink_properties=device.description=Badge_TikTok", "rate=48000", "channels=2")
            print(f"Created module {module}; capture badge_tiktok.monitor")
    elif args.action == "list":
        for stream in json.loads(pactl("--format=json", "list", "sink-inputs")):
            props = stream.get("properties", {})
            print(stream["index"], props.get("application.name", "?"), props.get("media.name", ""))
    elif args.action == "move":
        if args.value is None or not args.value.isdigit():
            parser.error("move requires the numeric stream ID shown by list")
        pactl("move-sink-input", args.value, "badge_tiktok")
        print("Moved selected stream to badge_tiktok; it will be silent on the PC")
    else:
        # PulseAudio-on-PipeWire can omit module indices from JSON module lists.
        # Sink owner_module remains available and identifies our exact sink.
        sinks = json.loads(pactl("--format=json", "list", "sinks"))
        for sink in sinks:
            if sink["name"] == "badge_tiktok":
                pactl("unload-module", str(sink["owner_module"]))
                print("Removed badge_tiktok")


if __name__ == "__main__":
    main()
