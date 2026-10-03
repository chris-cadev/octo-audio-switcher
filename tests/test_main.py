import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _stub_ulauncher():
    """Stub ulauncher API enough to import `main` and assert on UI objects."""

    class _Stub:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ExtensionResultItem(_Stub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.icon = kwargs.get("icon")
            self.name = kwargs.get("name")
            self.description = kwargs.get("description")
            self.on_enter = kwargs.get("on_enter")

    class _ExtensionCustomAction(_Stub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.data = args[0] if args else kwargs.get("data")
            self.keep_app_open = kwargs.get("keep_app_open", False)

    class _RenderResultListAction(_Stub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.items = args[0] if args else kwargs.get("items", [])

    class _HideWindowAction(_Stub):
        pass

    modules = {
        "ulauncher": types.ModuleType("ulauncher"),
        "ulauncher.api": types.ModuleType("ulauncher.api"),
        "ulauncher.api.client": types.ModuleType("ulauncher.api.client"),
        "ulauncher.api.client.Extension": types.ModuleType("ulauncher.api.client.Extension"),
        "ulauncher.api.client.EventListener": types.ModuleType("ulauncher.api.client.EventListener"),
        "ulauncher.api.shared": types.ModuleType("ulauncher.api.shared"),
        "ulauncher.api.shared.event": types.ModuleType("ulauncher.api.shared.event"),
        "ulauncher.api.shared.item": types.ModuleType("ulauncher.api.shared.item"),
        "ulauncher.api.shared.item.ExtensionResultItem": types.ModuleType(
            "ulauncher.api.shared.item.ExtensionResultItem"),
        "ulauncher.api.shared.action": types.ModuleType("ulauncher.api.shared.action"),
        "ulauncher.api.shared.action.RenderResultListAction": types.ModuleType(
            "ulauncher.api.shared.action.RenderResultListAction"),
        "ulauncher.api.shared.action.ExtensionCustomAction": types.ModuleType(
            "ulauncher.api.shared.action.ExtensionCustomAction"),
        "ulauncher.api.shared.action.HideWindowAction": types.ModuleType(
            "ulauncher.api.shared.action.HideWindowAction"),
    }
    modules["ulauncher.api.client.Extension"].Extension = _Stub
    modules["ulauncher.api.client.EventListener"].EventListener = _Stub
    modules["ulauncher.api.shared.event"].KeywordQueryEvent = _Stub
    modules["ulauncher.api.shared.event"].ItemEnterEvent = _Stub
    modules["ulauncher.api.shared.item.ExtensionResultItem"].ExtensionResultItem = _ExtensionResultItem
    modules["ulauncher.api.shared.action.RenderResultListAction"].RenderResultListAction = _RenderResultListAction
    modules["ulauncher.api.shared.action.ExtensionCustomAction"].ExtensionCustomAction = _ExtensionCustomAction
    modules["ulauncher.api.shared.action.HideWindowAction"].HideWindowAction = _HideWindowAction
    sys.modules.update(modules)


_stub_ulauncher()

import main


# Captured from `pactl list cards` for a real connected Bluetooth headset
# (Sennheiser MOMENTUM 4, card bluez_card.80_C3_BA_1F_73_9E).
REAL_CARD_PROFILES_OUTPUT = """\
Card #1550
\tName: bluez_card.80_C3_BA_1F_73_9E
\tDriver: module-bluez5-device.c
\tOwner Module: n/a
\tProperties:
\t\tdevice.description = "MOMENTUM 4"
\tProfiles:
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\t\theadset-head-unit: Headset Head Unit (HSP/HFP) (sinks: 1, sources: 1, priority: 1, available: yes)
\t\ta2dp-sink-sbc: High Fidelity Playback (A2DP Sink, codec SBC) (sinks: 1, sources: 0, priority: 18, available: yes)
\t\ta2dp-sink-sbc_xq: High Fidelity Playback (A2DP Sink, codec SBC-XQ) (sinks: 1, sources: 0, priority: 17, available: yes)
\t\ta2dp-sink-aptx: High Fidelity Playback (A2DP Sink, codec aptX) (sinks: 1, sources: 0, priority: 19, available: yes)
\t\ta2dp-sink: High Fidelity Playback (A2DP Sink, codec aptX HD) (sinks: 1, sources: 0, priority: 20, available: yes)
\tActive Profile: a2dp-sink-sbc_xq
\tPorts:
\t\theadset-output: Headset (type: Headset, priority: 0, latency offset: 0 usec, available)
"""

WPCTL_STATUS_OUTPUT = """\
PipeWire 'pipewire-0' [1.0.5, chris@host, cookie:1234567890]
 └─ Clients:
        32. WirePlumber                         [1.0.5, chris@host, pid:1000]

Audio
 ├─ Devices:
 │      40. alsa_card.pci-0000_00_1f.3         [alsa]
 │      41. bluez_card.80_C3_BA_1F_73_9E       [bluez5]
 ├─ Sinks:
 │  *   55. alsa_output.pci-0000_00_1f.3.analog-stereo  [vol: 0.40]
 │      88. bluez_output.80_C3_BA_1F_73_9E.1   [vol: 0.80]
 │      90. bluez_output.80_C3_BA_1F_73_9E.2   [vol: 0.80]
 ├─ Sources:
 │  *   56. alsa_input.pci-0000_00_1f.3.analog-stereo

Video
 ├─ Devices:
 └─ Sinks:

Settings
 └─ Default Configured Node Names:
        0. Audio/Sink    alsa_output.pci-0000_00_1f.3.analog-stereo
"""

WPCTL_INSPECT_ALSA = """\
id 55, type PipeWire:Interface:Node
    node.name = "alsa_output.pci-0000_00_1f.3.analog-stereo"
    media.class = "Audio/Sink"
"""

WPCTL_INSPECT_BT_HFP = """\
id 88, type PipeWire:Interface:Node
    node.name = "bluez_output.80_C3_BA_1F_73_9E.1"
    api.bluez5.profile = "hfp-handsfree"
    device.id = "117"
    media.class = "Audio/Sink"
"""

WPCTL_INSPECT_BT_A2DP = """\
id 90, type PipeWire:Interface:Node
    node.name = "bluez_output.80_C3_BA_1F_73_9E.2"
    api.bluez5.profile = "a2dp-sink"
    device.id = "117"
"""

WPCTL_INSPECT_CARD = """\
id 117, type PipeWire:Interface:Device
    device.name = "bluez_card.80_C3_BA_1F_73_9E"
"""

PW_DUMP_OUTPUT = json.dumps([
    {
        "id": 55,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {"node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
    },
    {
        "id": 88,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {
            "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
            "api.bluez5.profile": "hfp-handsfree",
            "device.id": "117",
        }},
    },
    {
        "id": 90,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {
            "node.name": "bluez_output.80_C3_BA_1F_73_9E.2",
            "api.bluez5.profile": "a2dp-sink",
            "device.id": "117",
        }},
    },
    {"id": 1, "type": "PipeWire:Interface:Client", "info": {"props": {}}},
])


def _cp(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def _inspect_by_id(node_id):
    mapping = {
        "55": WPCTL_INSPECT_ALSA,
        "88": WPCTL_INSPECT_BT_HFP,
        "90": WPCTL_INSPECT_BT_A2DP,
        "117": WPCTL_INSPECT_CARD,
    }
    return mapping.get(str(node_id), "")


def _run_wpctl_inspect(args, **kwargs):
    return _cp(args, stdout=_inspect_by_id(args[-1]))


def _hfp_graph(node_id=55):
    """Props for a BT sink stuck on HFP and its bluez card."""
    sink_props = {
        "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
        "api.bluez5.profile": "hfp-handsfree",
        "device.id": "117",
    }
    card_props = {"device.name": "bluez_card.80_C3_BA_1F_73_9E"}
    return sink_props, card_props


class ListCardProfilesTest(unittest.TestCase):
    def test_parses_priority_per_profile(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, REAL_CARD_PROFILES_OUTPUT)):
            profiles = main.list_card_profiles("bluez_card.80_C3_BA_1F_73_9E")
        self.assertEqual(profiles, {
            "off": 0,
            "headset-head-unit": 1,
            "a2dp-sink-sbc": 18,
            "a2dp-sink-sbc_xq": 17,
            "a2dp-sink-aptx": 19,
            "a2dp-sink": 20,
        })

    def test_missing_pactl_returns_empty_dict(self):
        with patch("main.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(main.list_card_profiles("bluez_card.x"), {})

    def test_pactl_failure_returns_empty_dict(self):
        with patch("main.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pactl")):
            self.assertEqual(main.list_card_profiles("bluez_card.x"), {})

    def test_unknown_card_returns_empty_dict(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, REAL_CARD_PROFILES_OUTPUT)):
            self.assertEqual(main.list_card_profiles("bluez_card.NOPE"), {})


class ChooseA2dpTargetTest(unittest.TestCase):
    def test_prefers_saved_profile_when_still_available(self):
        available = {"a2dp-sink": 20, "a2dp-sink-aptx": 19}
        self.assertEqual(
            main.choose_a2dp_target(available, saved="a2dp-sink-aptx"),
            "a2dp-sink-aptx",
        )

    def test_ignores_saved_profile_if_no_longer_available(self):
        available = {"a2dp-sink": 20}
        self.assertEqual(
            main.choose_a2dp_target(available, saved="a2dp-sink-ldac"),
            "a2dp-sink",
        )

    def test_ignores_saved_profile_if_not_a2dp(self):
        available = {"a2dp-sink": 20, "headset-head-unit": 1}
        self.assertEqual(
            main.choose_a2dp_target(available, saved="headset-head-unit"),
            "a2dp-sink",
        )

    def test_prefers_generic_a2dp_sink_when_present(self):
        available = {
            "a2dp-sink-sbc": 18, "a2dp-sink-aptx": 19, "a2dp-sink": 20,
        }
        self.assertEqual(main.choose_a2dp_target(available), "a2dp-sink")

    def test_falls_back_to_highest_priority_variant_when_generic_absent(self):
        available = {"a2dp-sink-sbc": 18, "a2dp-sink-aptx": 19}
        self.assertEqual(main.choose_a2dp_target(available), "a2dp-sink-aptx")

    def test_returns_none_when_no_a2dp_profile_available(self):
        available = {"off": 0, "headset-head-unit": 1}
        self.assertIsNone(main.choose_a2dp_target(available))

    def test_returns_none_when_available_empty(self):
        self.assertIsNone(main.choose_a2dp_target({}))


class ParsedWpctlStatusTest(unittest.TestCase):
    def test_parses_sinks_and_current_marker(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, WPCTL_STATUS_OUTPUT)):
            data = main.parsed_wpctl_status()
        sinks = data["Audio"]["Sinks"]
        self.assertEqual(sinks["current"], 55)
        # Parser keeps the full description text after the index.
        self.assertEqual(
            sinks["list"][55],
            "alsa_output.pci-0000_00_1f.3.analog-stereo  [vol: 0.40]",
        )
        self.assertEqual(
            sinks["list"][88],
            "bluez_output.80_C3_BA_1F_73_9E.1   [vol: 0.80]",
        )
        self.assertEqual(
            sinks["list"][90],
            "bluez_output.80_C3_BA_1F_73_9E.2   [vol: 0.80]",
        )

    def test_empty_status_returns_empty_structure(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, "")):
            data = main.parsed_wpctl_status()
        self.assertEqual(dict(data.get("Audio", {}).get("Sinks", {}).get("list", {})), {})


class GetNodePropertiesTest(unittest.TestCase):
    def test_parses_wpctl_inspect_key_values(self):
        with patch("main.subprocess.run", side_effect=_run_wpctl_inspect):
            props = main.get_node_properties(88)
        self.assertEqual(props["node.name"], "bluez_output.80_C3_BA_1F_73_9E.1")
        self.assertEqual(props["api.bluez5.profile"], "hfp-handsfree")
        self.assertEqual(props["device.id"], "117")

    def test_missing_binary_returns_empty_dict(self):
        with patch("main.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(main.get_node_properties(88), {})

    def test_inspect_failure_returns_empty_dict(self):
        with patch("main.subprocess.run", side_effect=subprocess.CalledProcessError(1, "wpctl")):
            self.assertEqual(main.get_node_properties(88), {})


class GetAllNodePropertiesTest(unittest.TestCase):
    def test_parses_pw_dump_nodes_only(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, PW_DUMP_OUTPUT)):
            props = main.get_all_node_properties()
        self.assertIn(55, props)
        self.assertIn(88, props)
        self.assertIn(90, props)
        self.assertNotIn(1, props)
        self.assertEqual(props[88]["api.bluez5.profile"], "hfp-handsfree")

    def test_missing_pw_dump_returns_none(self):
        with patch("main.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(main.get_all_node_properties())

    def test_invalid_json_returns_none(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, "not-json")):
            self.assertIsNone(main.get_all_node_properties())

    def test_non_list_json_returns_none(self):
        with patch("main.subprocess.run", side_effect=lambda a, **k: _cp(a, '{"id": 1}')):
            self.assertIsNone(main.get_all_node_properties())


class GetBluetoothLowQualityLabelTest(unittest.TestCase):
    def test_hfp_profile_from_snapshot(self):
        props = {88: {
            "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
            "api.bluez5.profile": "hfp-handsfree",
        }}
        self.assertEqual(main.get_bluetooth_low_quality_label(88, props), "HFP")

    def test_handsfree_variant_maps_to_hfp(self):
        props = {88: {
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "hfp-handsfree",
        }}
        self.assertEqual(main.get_bluetooth_low_quality_label(88, props), "HFP")

    def test_headset_head_unit_maps_to_hsp(self):
        # bluez5 name for the headset profile; code classifies via "headset".
        props = {88: {
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "headset-head-unit",
        }}
        self.assertEqual(main.get_bluetooth_low_quality_label(88, props), "HSP")

    def test_hsp_profile_from_snapshot(self):
        props = {88: {
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "headset-headset",
        }}
        self.assertEqual(main.get_bluetooth_low_quality_label(88, props), "HSP")

    def test_unknown_non_a2dp_profile_returns_raw_name(self):
        props = {88: {
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "custom-profile",
        }}
        self.assertEqual(main.get_bluetooth_low_quality_label(88, props), "custom-profile")

    def test_a2dp_profile_returns_none(self):
        props = {88: {
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "a2dp-sink-aptx",
        }}
        self.assertIsNone(main.get_bluetooth_low_quality_label(88, props))

    def test_non_bluetooth_sink_returns_none(self):
        props = {55: {"node.name": "alsa_output.pci.analog-stereo"}}
        self.assertIsNone(main.get_bluetooth_low_quality_label(55, props))

    def test_missing_profile_returns_none(self):
        props = {88: {"node.name": "bluez_output.x"}}
        self.assertIsNone(main.get_bluetooth_low_quality_label(88, props))

    def test_falls_back_to_inspect_when_missing_from_snapshot(self):
        with patch("main.get_node_properties", return_value={
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "hfp-handsfree",
        }) as mock_inspect:
            label = main.get_bluetooth_low_quality_label(88, {})
        self.assertEqual(label, "HFP")
        mock_inspect.assert_called_once_with(88)

    def test_no_snapshot_uses_inspect(self):
        with patch("main.get_node_properties", return_value={
            "node.name": "bluez_output.x",
            "api.bluez5.profile": "a2dp-sink",
        }):
            self.assertIsNone(main.get_bluetooth_low_quality_label(88))


class GetSavedCardProfileTest(unittest.TestCase):
    def _write_profile(self, tmpdir, content):
        wp = Path(tmpdir) / "wireplumber"
        wp.mkdir(parents=True, exist_ok=True)
        (wp / "default-profile").write_text(content)

    def test_reads_saved_a2dp_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(tmp, "bluez_card.80_C3_BA_1F_73_9E=a2dp-sink-aptx\n")
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                self.assertEqual(
                    main.get_saved_card_profile("bluez_card.80_C3_BA_1F_73_9E"),
                    "a2dp-sink-aptx",
                )

    def test_strips_inline_hash_and_semicolon_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(
                tmp,
                "bluez_card.X=a2dp-sink-aptx # set manually\n"
                "bluez_card.Y=a2dp-sink-ldac ; also fine\n",
            )
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                self.assertEqual(main.get_saved_card_profile("bluez_card.X"), "a2dp-sink-aptx")
                self.assertEqual(main.get_saved_card_profile("bluez_card.Y"), "a2dp-sink-ldac")

    def test_skips_comment_and_section_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(
                tmp,
                "[section]\n# comment\n; other\nbluez_card.Z=a2dp-sink\n",
            )
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                self.assertEqual(main.get_saved_card_profile("bluez_card.Z"), "a2dp-sink")

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                self.assertIsNone(main.get_saved_card_profile("bluez_card.X"))

    def test_missing_card_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(tmp, "bluez_card.OTHER=a2dp-sink\n")
            with patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                self.assertIsNone(main.get_saved_card_profile("bluez_card.MISSING"))


class EnsureHighQualityProfileTest(unittest.TestCase):
    def test_successful_switch_returns_none_and_does_not_poll(self):
        sink_props, card_props = _hfp_graph()

        def fake_props(node_id):
            return card_props if node_id == "117" else sink_props

        self.assertFalse(hasattr(main, "find_a2dp_sink"))
        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"a2dp-sink": 20}), \
             patch("main.get_saved_card_profile", return_value=None), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)

        mock_run.assert_called_once_with(
            ["pactl", "set-card-profile", "bluez_card.80_C3_BA_1F_73_9E", "a2dp-sink"],
            check=True,
        )
        self.assertIsNone(result)

    def test_uses_saved_profile_when_available(self):
        sink_props, card_props = _hfp_graph()

        def fake_props(node_id):
            return card_props if node_id == "117" else sink_props

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"a2dp-sink": 20, "a2dp-sink-aptx": 19}), \
             patch("main.get_saved_card_profile", return_value="a2dp-sink-aptx"), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)

        mock_run.assert_called_once_with(
            ["pactl", "set-card-profile", "bluez_card.80_C3_BA_1F_73_9E", "a2dp-sink-aptx"],
            check=True,
        )
        self.assertIsNone(result)

    def test_falls_back_to_highest_priority_variant(self):
        sink_props, card_props = _hfp_graph()

        def fake_props(node_id):
            return card_props if node_id == "117" else sink_props

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"a2dp-sink-aptx": 19, "a2dp-sink-sbc": 18}), \
             patch("main.get_saved_card_profile", return_value=None), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)

        mock_run.assert_called_once_with(
            ["pactl", "set-card-profile", "bluez_card.80_C3_BA_1F_73_9E", "a2dp-sink-aptx"],
            check=True,
        )
        self.assertIsNone(result)

    def test_no_a2dp_available_returns_original_id_without_pactl(self):
        sink_props, card_props = _hfp_graph()

        def fake_props(node_id):
            return card_props if node_id == "117" else sink_props

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"headset-head-unit": 1, "off": 0}), \
             patch("main.get_saved_card_profile", return_value=None), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)

        mock_run.assert_not_called()
        self.assertEqual(result, 55)

    def test_failed_switch_returns_original_id(self):
        sink_props, card_props = _hfp_graph()

        def fake_props(node_id):
            return card_props if node_id == "117" else sink_props

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"a2dp-sink": 20}), \
             patch("main.get_saved_card_profile", return_value=None), \
             patch("main.subprocess.run", side_effect=FileNotFoundError):
            result = main.ensure_high_quality_profile(55)
        self.assertEqual(result, 55)

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.list_card_profiles", return_value={"a2dp-sink": 20}), \
             patch("main.get_saved_card_profile", return_value=None), \
             patch("main.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pactl")):
            result = main.ensure_high_quality_profile(55)
        self.assertEqual(result, 55)

    def test_already_a2dp_returns_original_id_without_pactl(self):
        props = {
            "node.name": "bluez_output.80_C3_BA_1F_73_9E.2",
            "api.bluez5.profile": "a2dp-sink",
        }
        with patch("main.get_node_properties", return_value=props), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(70)
        mock_run.assert_not_called()
        self.assertEqual(result, 70)

    def test_non_bluetooth_sink_returns_original_id(self):
        with patch("main.get_node_properties", return_value={"node.name": "alsa_output.x"}), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)
        mock_run.assert_not_called()
        self.assertEqual(result, 55)

    def test_missing_device_id_returns_original_id(self):
        props = {
            "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
            "api.bluez5.profile": "headset-head-unit",
        }
        with patch("main.get_node_properties", return_value=props), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)
        mock_run.assert_not_called()
        self.assertEqual(result, 55)

    def test_non_bluez_card_returns_original_id(self):
        def fake_props(node_id):
            if node_id == "117":
                return {"device.name": "alsa_card.pci-0000_00_1f.3"}
            return {
                "node.name": "bluez_output.x",
                "api.bluez5.profile": "headset-head-unit",
                "device.id": "117",
            }

        with patch("main.get_node_properties", side_effect=fake_props), \
             patch("main.subprocess.run") as mock_run:
            result = main.ensure_high_quality_profile(55)
        mock_run.assert_not_called()
        self.assertEqual(result, 55)


class BuildSinkItemsTest(unittest.TestCase):
    def test_tags_low_quality_bt_and_sets_keep_app_open(self):
        all_props = {
            88: {
                "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
                "api.bluez5.profile": "hfp-handsfree",
            },
            55: {"node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"},
        }
        status = {
            "Audio": {"Sinks": {
                "list": {
                    55: "alsa_output.pci-0000_00_1f.3.analog-stereo",
                    88: "bluez_output.80_C3_BA_1F_73_9E.1",
                },
                "current": 55,
            }}
        }
        with patch("main.parsed_wpctl_status", return_value=status), \
             patch("main.get_all_node_properties", return_value=all_props):
            items = main._build_sink_items()

        self.assertEqual(len(items), 2)
        alsa, bt = items[0], items[1]
        self.assertIn("* 55", alsa.name)
        self.assertNotIn("[low quality]", alsa.name)
        self.assertEqual(alsa.description, "Switch to this audio sink")
        self.assertFalse(alsa.on_enter.keep_app_open)
        self.assertEqual(alsa.on_enter.data, {
            "sink_id": 55,
            "sink_name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
        })

        self.assertIn("[low quality]", bt.name)
        self.assertIn("88", bt.name)
        self.assertEqual(bt.description, "Currently on HFP; selecting will switch to A2DP")
        self.assertTrue(bt.on_enter.keep_app_open)
        self.assertEqual(bt.on_enter.data, {
            "sink_id": 88,
            "sink_name": "bluez_output.80_C3_BA_1F_73_9E.1",
        })

    def test_a2dp_bt_not_tagged_and_keep_open_false(self):
        status = {
            "Audio": {"Sinks": {
                "list": {90: "bluez_output.80_C3_BA_1F_73_9E.2"},
                "current": 90,
            }}
        }
        all_props = {
            90: {
                "node.name": "bluez_output.80_C3_BA_1F_73_9E.2",
                "api.bluez5.profile": "a2dp-sink",
            },
        }
        with patch("main.parsed_wpctl_status", return_value=status), \
             patch("main.get_all_node_properties", return_value=all_props):
            items = main._build_sink_items()

        self.assertEqual(len(items), 1)
        self.assertNotIn("[low quality]", items[0].name)
        self.assertTrue(items[0].name.startswith("* 90"))
        self.assertFalse(items[0].on_enter.keep_app_open)

    def test_empty_status_returns_no_items(self):
        with patch("main.parsed_wpctl_status", return_value={"Audio": {"Sinks": {"list": {}, "current": None}}}), \
             patch("main.get_all_node_properties", return_value=None):
            self.assertEqual(main._build_sink_items(), [])


class KeywordQueryEventListenerTest(unittest.TestCase):
    def test_renders_items_from_builder(self):
        sentinel = object()
        with patch("main._build_sink_items", return_value=[sentinel]):
            result = main.KeywordQueryEventListener().on_event(MagicMock(), MagicMock())
        self.assertEqual(result.items, [sentinel])

    def test_empty_list_shows_no_sinks_message(self):
        with patch("main._build_sink_items", return_value=[]):
            result = main.KeywordQueryEventListener().on_event(MagicMock(), MagicMock())
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].name, "No audio sinks found")

    def test_builder_error_shows_error_item(self):
        with patch("main._build_sink_items", side_effect=RuntimeError("boom")):
            result = main.KeywordQueryEventListener().on_event(MagicMock(), MagicMock())
        self.assertEqual(result.items[0].name, "Error retrieving sinks")
        self.assertEqual(result.items[0].description, "boom")


class ItemEnterEventListenerTest(unittest.TestCase):
    def _event(self, sink_id=55, sink_name="alsa_output.x"):
        event = MagicMock()
        event.get_data.return_value = {"sink_id": sink_id, "sink_name": sink_name}
        return event

    def test_profile_switch_rerenders_and_skips_set_default(self):
        sentinel = object()
        with patch("main.ensure_high_quality_profile", return_value=None), \
             patch("main._build_sink_items", return_value=[sentinel]), \
             patch("main.subprocess.run") as mock_run:
            result = main.ItemEnterEventListener().on_event(self._event(), MagicMock())

        mock_run.assert_not_called()
        self.assertEqual(result.items, [sentinel])

    def test_normal_switch_calls_set_default(self):
        with patch("main.ensure_high_quality_profile", return_value=55), \
             patch("main.subprocess.run") as mock_run:
            result = main.ItemEnterEventListener().on_event(
                self._event(55, "alsa_output.pci-0000_00_1f.3.analog-stereo"),
                MagicMock(),
            )

        mock_run.assert_called_once_with(["wpctl", "set-default", "55"], check=True)
        self.assertIn("Switched to 55", result.items[0].name)
        self.assertEqual(result.items[0].description, "Audio sink changed successfully")

    def test_set_default_failure_shows_failed_message(self):
        with patch("main.ensure_high_quality_profile", return_value=55), \
             patch("main.subprocess.run", side_effect=subprocess.CalledProcessError(1, "wpctl")):
            result = main.ItemEnterEventListener().on_event(self._event(), MagicMock())

        self.assertEqual(result.items[0].name, "Failed to switch sink")


class EndToEndMockedHardwareTest(unittest.TestCase):
    """Happy path: HFP sink listed, selected, card switched, list re-rendered."""

    @staticmethod
    def _event_from_action(action):
        event = MagicMock()
        event.get_data.return_value = action.data
        return event

    def test_hfp_select_switches_card_then_rerenders_without_set_default(self):
        switched = {"done": False}

        def fake_run(args, **kwargs):
            if args[:2] == ["wpctl", "status"]:
                if switched["done"]:
                    status = """Audio
 ├─ Sinks:
 │  *   55. alsa_output.pci-0000_00_1f.3.analog-stereo
 │      90. bluez_output.80_C3_BA_1F_73_9E.2
"""
                else:
                    status = WPCTL_STATUS_OUTPUT
                return _cp(args, status)
            if args[:2] == ["wpctl", "inspect"]:
                if switched["done"] and args[-1] in ("88", "90"):
                    return _cp(args, WPCTL_INSPECT_BT_A2DP if args[-1] == "90" else "")
                return _cp(args, stdout=_inspect_by_id(args[-1]))
            if args[:2] == ["pactl", "list"]:
                return _cp(args, REAL_CARD_PROFILES_OUTPUT)
            if args[:2] == ["pw-dump"]:
                if switched["done"]:
                    dump = json.dumps([
                        {"id": 55, "type": "PipeWire:Interface:Node",
                         "info": {"props": {"node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}}},
                        {"id": 90, "type": "PipeWire:Interface:Node",
                         "info": {"props": {
                             "node.name": "bluez_output.80_C3_BA_1F_73_9E.2",
                             "api.bluez5.profile": "a2dp-sink",
                             "device.id": "117",
                         }}},
                    ])
                    return _cp(args, dump)
                dump = json.dumps([
                    {"id": 55, "type": "PipeWire:Interface:Node",
                     "info": {"props": {"node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}}},
                    {"id": 88, "type": "PipeWire:Interface:Node",
                     "info": {"props": {
                         "node.name": "bluez_output.80_C3_BA_1F_73_9E.1",
                         "api.bluez5.profile": "hfp-handsfree",
                         "device.id": "117",
                     }}},
                ])
                return _cp(args, dump)
            if args[:2] == ["pactl", "set-card-profile"]:
                switched["done"] = True
                return _cp(args)
            if args[:2] == ["wpctl", "set-default"]:
                raise AssertionError("set-default must not run after profile switch")
            raise AssertionError(f"unexpected command: {args}")

        with patch("main.subprocess.run", side_effect=fake_run), \
             patch("main.get_saved_card_profile", return_value=None):
            listed = main.KeywordQueryEventListener().on_event(MagicMock(), MagicMock())
            names = [i.name for i in listed.items]
            self.assertTrue(any("[low quality]" in n and "88" in n for n in names))
            self.assertTrue(any("HFP" in (i.description or "") for i in listed.items))
            low_quality_item = next(i for i in listed.items if "[low quality]" in i.name)
            self.assertTrue(low_quality_item.on_enter.keep_app_open)

            rerendered = main.ItemEnterEventListener().on_event(
                self._event_from_action(low_quality_item.on_enter), MagicMock())

        self.assertTrue(switched["done"])
        names_after = [i.name for i in rerendered.items]
        self.assertTrue(any("bluez_output" in n and "[low quality]" not in n for n in names_after))
        self.assertTrue(any("90" in n for n in names_after))


if __name__ == "__main__":
    unittest.main()
