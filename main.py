import subprocess
import os
import re
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


def get_bluetooth_low_quality_label(sink_id, node_name=None):
    """Return a short label (e.g. 'HFP') if this sink is a Bluetooth node
    currently on a non-A2DP profile, else None.

    The label is derived from `api.bluez5.profile` so it stays accurate
    across HSP, HFP head-unit, and HFP audio-gateway variants.
    """
    if node_name is None:
        node_name = get_node_properties(sink_id).get("node.name", "")
    if not node_name.startswith("bluez_output."):
        return None
    props = get_node_properties(sink_id)
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
                    return value.split("#")[0].strip()
    except OSError:
        pass
    return None


def list_card_profiles(card_name):
    """Return the available profile names for a pactl card."""
    try:
        result = subprocess.run(
            ["pactl", "list", "cards"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    profiles = []
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
            m = re.match(r"\t\t([\w\-_]+):", line)
            if m:
                profiles.append(m.group(1))
    return profiles


def ensure_high_quality_profile(sink_id):
    """For a Bluetooth sink stuck on HFP/HSP, switch its card to the best A2DP profile.

    Returns the sink ID to use as default. May differ from the input if the
    profile change replaced the sink node with a fresh A2DP one."""
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

    # Prefer the codec the user last chose for this specific card, if it's
    # still an A2DP profile and the card still exposes it. Otherwise fall
    # back to plain a2dp-sink and let PipeWire pick the codec from its config.
    saved = get_saved_card_profile(card_name)
    if saved and "a2dp" in saved.lower() and saved in available:
        target = saved
    elif "a2dp-sink" in available:
        target = "a2dp-sink"
    else:
        target = None

    if not target:
        logger.warning("No A2DP profile available for %s (have: %s)", card_name, available)
        return sink_id

    logger.info("Switching %s from %s to %s", card_name, current_profile or "?", target)
    try:
        subprocess.run(["pactl", "set-card-profile", card_name, target], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Failed to set card profile %s on %s: %s", target, card_name, e)
        return sink_id

    # Profile switched — the old HFP sink is gone. Return None so the caller
    # re-renders the list and the user picks the new A2DP sink.
    return None


class SinkSwitcherExtension(Extension):
    def __init__(self):
        super(SinkSwitcherExtension, self).__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(ItemEnterEvent, ItemEnterEventListener())


class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        items = []

        try:
            data = parsed_wpctl_status()

            audio_sinks = data.get("Audio", {}).get("Sinks", {})
            sinks_list = audio_sinks.get("list", {})
            current_id = audio_sinks.get("current")

            if not sinks_list:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name='No audio sinks found',
                    description='No sinks available to switch',
                    on_enter=HideWindowAction()
                ))
                return RenderResultListAction(items)

            for sink_id, sink_desc in sinks_list.items():
                marker = "* " if sink_id == current_id else "  "
                label = f"{marker}{sink_id} → {sink_desc}"
                is_bluetooth = sink_desc.startswith("bluez_output.")
                low_quality = get_bluetooth_low_quality_label(sink_id, sink_desc) if is_bluetooth else None
                if low_quality:
                    label += "  [low quality]"
                    description = f"Currently on {low_quality}; selecting will switch to A2DP"
                    keep_open = True
                else:
                    description = "Switch to this audio sink"
                    keep_open = False
                data = {"sink_id": sink_id, "sink_name": sink_desc}
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name=label,
                    description=description,
                    on_enter=ExtensionCustomAction(data, keep_app_open=keep_open)
                ))

        except Exception as e:
            logger.exception("Error parsing wpctl status")
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name='Error retrieving sinks',
                description=str(e),
                on_enter=HideWindowAction()
            ))

        return RenderResultListAction(items)


class ItemEnterEventListener(EventListener):
    def _rerender_sink_list(self):
        """Re-parse wpctl status and return the updated sink list."""
        items = []
        data = parsed_wpctl_status()
        audio_sinks = data.get("Audio", {}).get("Sinks", {})
        sinks_list = audio_sinks.get("list", {})
        current_id = audio_sinks.get("current")
        for sink_id, sink_desc in sinks_list.items():
            marker = "* " if sink_id == current_id else "  "
            label = f"{marker}{sink_id} → {sink_desc}"
            is_bluetooth = sink_desc.startswith("bluez_output.")
            low_quality = get_bluetooth_low_quality_label(sink_id, sink_desc) if is_bluetooth else None
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
                on_enter=ExtensionCustomAction({"sink_id": sink_id, "sink_name": sink_desc}, keep_app_open=keep_open)
            ))
        return RenderResultListAction(items)

    def on_event(self, event, extension):
        data = event.get_data()
        sink_id = data.get("sink_id")
        sink_name = data.get("sink_name")

        try:
            new_sink_id = ensure_high_quality_profile(sink_id)
            if new_sink_id is None:
                # Profile was switched — re-render so user picks the new A2DP sink.
                return self._rerender_sink_list()
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
