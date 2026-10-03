import subprocess
import os
import re
import json
import logging
from collections import defaultdict

from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, ItemEnterEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.ExtensionCustomAction import ExtensionCustomAction
from ulauncher.api.shared.action.HideWindowAction import HideWindowAction

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


def parsed_wpctl_status():
    result = subprocess.run(['wpctl', 'status'], stdout=subprocess.PIPE, text=True)
    lines = result.stdout.splitlines()

    data = defaultdict(lambda: defaultdict(lambda: {"list": {}, "current": None}))
    section = None
    category = None

    def parse_entry(line):
        current = False
        if '*' in line:
            current = True
            line = line.replace('*', '', 1)
        line = line.strip(" │")
        match = re.match(r'(\d+)\.\s+(.*)', line)
        if match:
            idx, desc = match.groups()
            return int(idx), desc.strip(), current
        return None, None, False

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if re.match(r'^(Audio|Video|Settings)\s*$', line):
            section = line
            continue

        match_cat = re.match(r'[├└]─ ([\w\s]+):', line)
        if match_cat:
            category = match_cat.group(1).strip()
            continue

        if category in ["Sinks", "Sources", "Devices", "Sink endpoints", "Source endpoints", "Streams", "Default Configured Node Names"]:
            idx, desc, is_current = parse_entry(line)
            if idx is not None:
                if category == "Default Configured Node Names":
                    data[section][category][idx] = desc
                else:
                    data[section][category]["list"][idx] = desc
                    if is_current:
                        data[section][category]["current"] = idx

    return data


def get_node_properties(node_id):
    """Parse `wpctl inspect <id>` into a flat key/value dict."""
    try:
        result = subprocess.run(
            ["wpctl", "inspect", str(node_id)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    props = {}
    for line in result.stdout.splitlines():
        m = re.search(r'([\w\.\-]+)\s*=\s*"([^"]*)"', line)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def get_all_node_properties():
    """One `pw-dump` call -> {node_id: props} for every PipeWire node.

    Returns None if `pw-dump` is unavailable or its output can't be parsed,
    so callers can fall back to per-node `wpctl inspect` calls. This is a
    fresh snapshot, not a cache, so it carries no staleness risk.
    """
    try:
        result = subprocess.run(
            ["pw-dump"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=True,
        )
        objects = json.loads(result.stdout)
        if not isinstance(objects, list):
            return None
        return {
            obj["id"]: obj.get("info", {}).get("props", {})
            for obj in objects
            if obj.get("type") == "PipeWire:Interface:Node" and obj.get("id") is not None
        }
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def get_bluetooth_low_quality_label(sink_id, all_props=None):
    """Return a short label (e.g. 'HFP') if this sink is a Bluetooth node
    currently on a non-A2DP profile, else None.

    The label is derived from `api.bluez5.profile` so it stays accurate
    across HSP, HFP head-unit, and HFP audio-gateway variants.

    `all_props`, if given, is a pre-fetched {node_id: props} snapshot (see
    `get_all_node_properties`) used to avoid a per-sink `wpctl inspect`
    call. Falls back to inspecting this sink directly if it's missing from
    the snapshot (e.g. a status/snapshot race) or no snapshot was given.
    """
    props = (all_props or {}).get(sink_id)
    if props is None:
        props = get_node_properties(sink_id)
    if not props.get("node.name", "").startswith("bluez_output."):
        return None
    profile = props.get("api.bluez5.profile", "")
    if not profile or "a2dp" in profile.lower():
        return None
    lowered = profile.lower()
    if "handsfree" in lowered or "hfp" in lowered:
        return "HFP"
    if "headset" in lowered or "hsp" in lowered:
        return "HSP"
    return profile


def get_saved_card_profile(card_name):
    """Return the profile WirePlumber last persisted for this card, or None.

    WirePlumber records the user's last-chosen profile per card in
    `$XDG_STATE_HOME/wireplumber/default-profile`, e.g.:
        bluez_card.XX_XX_XX_XX_XX_XX=a2dp-sink-aptx

    This file is a WirePlumber 0.4 (media-session) mechanism. On 0.5+ the
    same data lives in the metadata store instead, so this returns None
    there and the caller falls back to the card's generic a2dp-sink
    profile, or its highest-priority a2dp-sink* variant.
    """
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(state_home, "wireplumber", "default-profile")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("[", "#", ";")):
                    continue
                key, sep, value = line.partition("=")
                if sep and key.strip() == card_name:
                    value = re.split(r"\s*[#;]", value, maxsplit=1)[0]
                    return value.strip()
    except OSError:
        pass
    return None


def list_card_profiles(card_name):
    """Return {profile_name: priority} for a pactl card.

    Requires `pactl` (pipewire-pulse), not just plain `wpctl`/pipewire.
    Expected `pactl list cards` shape for the target card:
        Card #3
            Name: bluez_card.XX_XX_XX_XX_XX_XX
            ...
            Profiles:
                a2dp-sink-sbc: High Fidelity Playback (A2DP Sink, codec SBC) (sinks: 1, sources: 0, priority: 18, available: yes)
                headset-head-unit: Headset Head Unit (HSP/HFP) (sinks: 1, sources: 1, priority: 1, available: yes)
    Each profile line is tab-indented twice under a "\tProfiles:" header,
    ending at the next line with fewer than two leading tabs. Higher
    `priority` means more preferred, per pactl/PipeWire convention.
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "cards"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    profiles = {}
    in_target = False
    in_profiles = False
    for line in result.stdout.splitlines():
        if re.match(r"^Card #", line):
            in_target = False
            in_profiles = False
            continue
        if line.strip() == f"Name: {card_name}":
            in_target = True
            continue
        if not in_target:
            continue
        if line.startswith("\tProfiles:"):
            in_profiles = True
            continue
        if in_profiles:
            if not line.startswith("\t\t"):
                in_profiles = False
                continue
            m = re.match(r"\t\t([\w\-_]+):.*\bpriority:\s*(\d+)", line)
            if m:
                profiles[m.group(1)] = int(m.group(2))
    return profiles


def choose_a2dp_target(available, saved=None):
    """Pick the A2DP profile to switch a card to.

    `available` is a {profile_name: priority} dict (see `list_card_profiles`).
    `saved` is the profile WirePlumber last persisted for this card, if any
    (see `get_saved_card_profile`). Preference order:
    1. `saved`, if it's still an A2DP profile and the card still exposes it.
    2. The generic `a2dp-sink` profile (codec picked by PipeWire's own
       `bluez5.codecs` config).
    3. The highest-priority `a2dp-sink*` variant the card reports, using
       the card's own stated priority — not list order or alphabetical sort.
    Returns None if the card exposes no A2DP profile at all.
    """
    if saved and "a2dp" in saved.lower() and saved in available:
        return saved
    if "a2dp-sink" in available:
        return "a2dp-sink"
    return max(
        (p for p in available if p.startswith("a2dp-sink")),
        key=lambda p: available[p], default=None,
    )


def ensure_high_quality_profile(sink_id):
    """For a Bluetooth sink stuck on HFP/HSP, switch its card to an A2DP profile.

    Returns sink_id when no profile change is needed or the switch failed
    (caller may still `wpctl set-default` that id). Returns None after a
    successful `pactl set-card-profile` so the caller re-renders the list
    instead of set-default on the destroyed HFP node.
    """
    props = get_node_properties(sink_id)
    if not props.get("node.name", "").startswith("bluez_output."):
        return sink_id

    current_profile = props.get("api.bluez5.profile", "").lower()
    if "a2dp" in current_profile:
        return sink_id

    device_id = props.get("device.id")
    if not device_id:
        return sink_id

    card_name = get_node_properties(device_id).get("device.name", "")
    if not card_name.startswith("bluez_card."):
        return sink_id

    available = list_card_profiles(card_name)
    saved = get_saved_card_profile(card_name)
    target = choose_a2dp_target(available, saved)
    if not target:
        logger.warning("No A2DP profile available for %s (have: %s)", card_name, list(available))
        return sink_id

    logger.info("Switching %s from %s to %s", card_name, current_profile or "?", target)
    try:
        subprocess.run(["pactl", "set-card-profile", card_name, target], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Failed to set card profile %s on %s: %s", target, card_name, e)
        return sink_id

    return None


def _build_sink_items():
    """Build sink list items from current wpctl status (shared by both listeners)."""
    data = parsed_wpctl_status()
    audio_sinks = data.get("Audio", {}).get("Sinks", {})
    sinks_list = audio_sinks.get("list", {})
    current_id = audio_sinks.get("current")
    all_props = get_all_node_properties()
    items = []
    for sink_id, sink_desc in sinks_list.items():
        marker = "* " if sink_id == current_id else "  "
        label = f"{marker}{sink_id} → {sink_desc}"
        low_quality = get_bluetooth_low_quality_label(sink_id, all_props)
        if low_quality:
            label += "  [low quality]"
            description = f"Currently on {low_quality}; selecting will switch to A2DP"
            keep_open = True
        else:
            description = "Switch to this audio sink"
            keep_open = False
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name=label,
            description=description,
            on_enter=ExtensionCustomAction(
                {"sink_id": sink_id, "sink_name": sink_desc},
                keep_app_open=keep_open,
            ),
        ))
    return items


class SinkSwitcherExtension(Extension):
    def __init__(self):
        super(SinkSwitcherExtension, self).__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(ItemEnterEvent, ItemEnterEventListener())


class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        try:
            items = _build_sink_items()
            if not items:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name='No audio sinks found',
                    description='No sinks available to switch',
                    on_enter=HideWindowAction()
                ))
            return RenderResultListAction(items)
        except Exception as e:
            logger.exception("Error parsing wpctl status")
            return RenderResultListAction([ExtensionResultItem(
                icon='images/icon.png',
                name='Error retrieving sinks',
                description=str(e),
                on_enter=HideWindowAction()
            )])


class ItemEnterEventListener(EventListener):
    def on_event(self, event, extension):
        data = event.get_data()
        sink_id = data.get("sink_id")
        sink_name = data.get("sink_name")

        try:
            new_sink_id = ensure_high_quality_profile(sink_id)
            if new_sink_id is None:
                # Profile switched — re-render so user picks the new A2DP sink.
                return RenderResultListAction(_build_sink_items())
            subprocess.run(["wpctl", "set-default", str(new_sink_id)], check=True)
            success_msg = f"Switched to {new_sink_id} → {sink_name}"
            logger.info(success_msg)
            return RenderResultListAction([ExtensionResultItem(
                icon='images/icon.png',
                name=success_msg,
                description="Audio sink changed successfully",
                on_enter=HideWindowAction()
            )])
        except Exception as e:
            logger.exception("Failed to switch sink")
            return RenderResultListAction([ExtensionResultItem(
                icon='images/icon.png',
                name="Failed to switch sink",
                description=str(e),
                on_enter=HideWindowAction()
            )])


if __name__ == '__main__':
    SinkSwitcherExtension().run()
