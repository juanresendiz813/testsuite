"""Unit tests for smoke suite environment setup helpers."""
import importlib
import itertools
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from behave.parser import parse_file

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_FEATURES = REPO_ROOT / "tests" / "smoke" / "features"


def _setup_stubs():
    # Stub behave
    behave_stub = types.ModuleType("behave")
    behave_stub.step = lambda *a, **kw: (lambda f: f)
    sys.modules["behave"] = behave_stub

    # Stub dogtail
    dogtail_stub = types.ModuleType("dogtail")
    tree_stub = types.ModuleType("dogtail.tree")
    tree_stub.root = MagicMock()
    sys.modules["dogtail"] = dogtail_stub
    sys.modules["dogtail.tree"] = tree_stub

    # Stub qecore
    qecore_stub = types.ModuleType("qecore")
    qecore_common_stub = types.ModuleType("qecore.common_steps")
    sys.modules["qecore"] = qecore_stub
    sys.modules["qecore.common_steps"] = qecore_common_stub

    # Stub steps and steps.app_support
    steps_stub = types.ModuleType("steps")
    steps_steps_stub = types.ModuleType("steps.steps")
    steps_steps_stub._dismiss_welcome_dialog = MagicMock()
    app_support_stub = types.ModuleType("steps.app_support")
    app_support_stub._IN_CONTAINER = False
    app_support_stub._ssh_run = MagicMock()
    sys.modules["steps"] = steps_stub
    sys.modules["steps.steps"] = steps_steps_stub
    sys.modules["steps.app_support"] = app_support_stub


def _run_before_all(in_container: bool):
    """Drive before_all and return every command it issued, as flat strings.

    Commands are normalised to strings so the assertions below describe the
    settings that must be applied, not the argv shape or call order.
    """
    _setup_stubs()
    sys.modules["steps.app_support"]._IN_CONTAINER = in_container
    mock_ssh_run = MagicMock(returncode=0, stdout="(true, 'true')")
    sys.modules["steps.app_support"]._ssh_run = mock_ssh_run

    with patch("time.sleep"), \
         patch("subprocess.run") as mock_run, \
         patch("tests.shared.ssh_config.populate_ssh_context"), \
         patch("builtins.open", MagicMock()):
        mock_run.return_value = MagicMock(returncode=0, stdout="(true, 'true')")

        env = importlib.import_module("tests.smoke.features.environment")
        env.before_all(MagicMock())

    issued = list(mock_run.call_args_list)
    if in_container:
        issued += list(mock_ssh_run.call_args_list)

    out = []
    for call in issued:
        if not call[0]:
            continue
        arg = call[0][0]
        out.append(" ".join(arg) if isinstance(arg, list) else str(arg))
    return out


def test_before_all_suppresses_idle_lock_on_the_vm():
    """Long smoke runs must not hit the lock screen: both settings get applied."""
    commands = _run_before_all(in_container=False)

    assert any("org.gnome.desktop.session" in c and "idle-delay" in c and "0" in c
               for c in commands), commands
    assert any("org.gnome.desktop.screensaver" in c and "lock-enabled" in c and "false" in c
               for c in commands), commands


def test_before_all_suppresses_idle_lock_from_inside_the_runner_container():
    """The container lane reaches the VM session over SSH, so the same two
    settings must still be applied rather than silently skipped."""
    commands = _run_before_all(in_container=True)

    assert any("idle-delay" in c and "0" in c for c in commands), commands
    assert any("lock-enabled" in c and "false" in c for c in commands), commands


# ── image family detection ────────────────────────────────────────────────────

def _load_environment():
    _setup_stubs()
    return importlib.import_module("tests.smoke.features.environment")


def _before_all_context(monkeypatch, **env):
    """Drive before_all with the given IMAGE/BASE_IMAGE and return the context.

    Image detection sits after the qecore availability check, so the sandbox
    module is stubbed as present; ``time.time`` jumps past the AT-SPI poll
    deadline so the hook does not spin for its 15 s budget.
    """
    for name in ("IMAGE", "BASE_IMAGE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _setup_stubs()
    sandbox_stub = types.ModuleType("qecore.sandbox")
    sandbox_stub.TestSandbox = MagicMock()
    sys.modules["qecore.sandbox"] = sandbox_stub
    context = MagicMock()
    with patch("time.sleep"), \
         patch("time.time", side_effect=itertools.count(0, 60)), \
         patch("subprocess.run") as mock_run, \
         patch("tests.shared.ssh_config.populate_ssh_context"), \
         patch("builtins.open", MagicMock()):
        mock_run.return_value = MagicMock(returncode=0, stdout="(true, 'true')")
        env_module = importlib.import_module("tests.smoke.features.environment")
        env_module.before_all(context)
    return context


class TestIsLtsImage:
    @pytest.mark.parametrize("image", [
        "ghcr.io/projectbluefin/bluefin-lts:stable",
        "ghcr.io/projectbluefin/bluefin-lts@sha256:" + "a" * 64,
        "ghcr.io/projectbluefin/bluefin-lts-hwe:testing",
        "ghcr.io/ublue-os/bluefin:lts",
        "ghcr.io/ublue-os/bluefin-dx:lts-testing",
        "GHCR.IO/ProjectBluefin/Bluefin-LTS:Stable",
    ])
    def test_matches_lts_images(self, image):
        env = _load_environment()
        assert env._is_lts_image(image) is True

    @pytest.mark.parametrize("image", [
        "ghcr.io/projectbluefin/bluefin:stable",
        "ghcr.io/projectbluefin/bluefin:latest",
        "ghcr.io/ublue-os/bluefin:stable",
        "ghcr.io/ublue-os/bluefin-dx:gts",
        "ghcr.io/projectbluefin/dakota:testing",
        "ghcr.io/juanresendiz813/testsuite-e2e:run-123",
        "",
    ])
    def test_rejects_other_images(self, image):
        env = _load_environment()
        assert env._is_lts_image(image) is False

    def test_before_all_flags_an_lts_image(self, monkeypatch):
        context = _before_all_context(
            monkeypatch, IMAGE="ghcr.io/projectbluefin/bluefin-lts:stable"
        )
        assert context.is_lts_image is True
        assert context.is_bluefin_image is True
        assert context.is_dakota_image is False

    def test_before_all_prefers_base_image_on_composed_runs(self, monkeypatch):
        """IMAGE is the composed testsuite-e2e ref on composed runs (#907)."""
        context = _before_all_context(
            monkeypatch,
            IMAGE="ghcr.io/projectbluefin/testsuite-e2e:run-123",
            BASE_IMAGE="ghcr.io/projectbluefin/bluefin-lts@sha256:" + "b" * 64,
        )
        assert context.is_lts_image is True
        assert context.is_bluefin_image is True

    def test_before_all_does_not_flag_full_bluefin(self, monkeypatch):
        context = _before_all_context(
            monkeypatch, IMAGE="ghcr.io/projectbluefin/bluefin:stable"
        )
        assert context.is_lts_image is False
        assert context.is_bluefin_image is True

    def test_before_all_defaults_to_not_lts_without_an_image_ref(self, monkeypatch):
        context = _before_all_context(monkeypatch)
        assert context.is_lts_image is False
        assert context.is_bluefin_image is True


# ── @requires_installed_extension ─────────────────────────────────────────────

COPYOUS = "copyous@boerdereinar.dev"
CAFFEINE = "caffeine@patapon.info"


def _scenario(tags, step_names, name="scenario"):
    return SimpleNamespace(
        name=name,
        tags=list(tags),
        effective_tags=set(tags),
        all_steps=[SimpleNamespace(name=step) for step in step_names],
        skip=MagicMock(),
    )


def _enabled_step(uuid):
    return f'GNOME extension "{uuid}" is enabled'


def _probe(returncode=None, side_effect=None):
    """Put the stubbed runner in container mode with an SSH probe stand-in."""
    app_support = sys.modules["steps.app_support"]
    app_support._IN_CONTAINER = True
    app_support._ssh_run = MagicMock(
        return_value=SimpleNamespace(returncode=returncode), side_effect=side_effect
    )
    return app_support._ssh_run


def _real_scenarios(feature_name):
    return parse_file(str(SMOKE_FEATURES / feature_name)).scenarios


class TestExtensionsAssertedBy:
    def test_reads_the_uuid_from_the_scenario_steps(self):
        env = _load_environment()
        scenario = _scenario(("extensions",), [
            "GNOME Shell is accessible via AT-SPI",
            _enabled_step(CAFFEINE),
            _enabled_step(CAFFEINE),
        ])
        assert env._extensions_asserted_by(scenario) == [CAFFEINE]

    def test_returns_nothing_without_an_enabled_step(self):
        env = _load_environment()
        scenario = _scenario(("extensions",), [
            "GNOME Shell is accessible via AT-SPI",
            f'GNOME extension "{CAFFEINE}" is installed',
        ])
        assert env._extensions_asserted_by(scenario) == []

    def test_background_steps_count(self):
        """copyous_stress.feature asserts the extension in its Background."""
        env = _load_environment()
        scenarios = _real_scenarios("copyous_stress.feature")
        assert scenarios
        for scenario in scenarios:
            assert env._extensions_asserted_by(scenario) == [COPYOUS], scenario.name

    def test_bluefin_extensions_feature_names_one_uuid_per_scenario(self):
        env = _load_environment()
        by_name = {
            scenario.name: env._extensions_asserted_by(scenario)
            for scenario in _real_scenarios("bluefin_extensions.feature")
        }
        assert by_name["Caffeine extension is enabled"] == [CAFFEINE]
        assert all(len(uuids) == 1 for uuids in by_name.values()), by_name

    def test_every_tagged_smoke_scenario_names_an_extension(self):
        """Authoring guard: the tag promises an extension the steps must name."""
        env = _load_environment()
        tagged = []
        for feature_path in sorted(SMOKE_FEATURES.glob("*.feature")):
            for scenario in parse_file(str(feature_path)).scenarios:
                tags = set(getattr(scenario, "effective_tags", scenario.tags))
                if env.REQUIRES_INSTALLED_EXTENSION_TAG in tags:
                    tagged.append((feature_path.name, scenario.name))
                    assert env._extensions_asserted_by(scenario), (
                        f"{feature_path.name}: {scenario.name!r} is tagged "
                        f"@{env.REQUIRES_INSTALLED_EXTENSION_TAG} but asserts no extension"
                    )
        assert tagged, "no smoke scenario carries the tag; the guard is vacuous"


class TestSkipWhenExtensionNotInstalled:
    def _lts_context(self):
        return SimpleNamespace(is_lts_image=True)

    def test_skips_on_lts_when_the_extension_directory_is_absent(self):
        env = _load_environment()
        ssh = _probe(returncode=1)
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)]
        )

        assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is True

        scenario.skip.assert_called_once()
        reason = scenario.skip.call_args[0][0]
        assert env.REQUIRES_INSTALLED_EXTENSION_TAG in reason
        assert COPYOUS in reason
        ssh.assert_called_once()
        assert ssh.call_args[0][0] == f"test -d {env.SYSTEM_EXTENSION_DIR}/{COPYOUS}"

    def test_runs_on_lts_when_the_extension_is_installed(self):
        """Installed but disabled (state=6, e.g. Caffeine) must still fail loudly."""
        env = _load_environment()
        _probe(returncode=0)
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(CAFFEINE)]
        )

        assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is False
        scenario.skip.assert_not_called()

    @pytest.mark.parametrize("returncode", [255, 124])
    def test_runs_when_the_probe_cannot_tell(self, returncode):
        env = _load_environment()
        _probe(returncode=returncode)
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)]
        )

        assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is False
        scenario.skip.assert_not_called()

    def test_runs_when_the_probe_times_out(self):
        env = _load_environment()
        _probe(side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30))
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)]
        )

        assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is False
        scenario.skip.assert_not_called()

    def test_never_probes_on_images_that_own_the_contract(self):
        """projectbluefin/bluefin, Dakota and Classic: a dropped extension fails."""
        env = _load_environment()
        ssh = _probe(returncode=1)
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)]
        )
        context = SimpleNamespace(is_lts_image=False)

        assert env._skip_when_extension_not_installed(context, scenario) is False
        scenario.skip.assert_not_called()
        ssh.assert_not_called()

    def test_ignores_untagged_scenarios(self):
        env = _load_environment()
        ssh = _probe(returncode=1)
        scenario = _scenario(("extensions",), [_enabled_step(COPYOUS)])

        assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is False
        scenario.skip.assert_not_called()
        ssh.assert_not_called()

    def test_probes_each_extension_once_per_run(self):
        env = _load_environment()
        ssh = _probe(returncode=1)
        context = self._lts_context()
        for name in ("burst churn", "null bytes"):
            scenario = _scenario(
                (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)], name
            )
            assert env._skip_when_extension_not_installed(context, scenario) is True
        assert ssh.call_count == 1

    @pytest.mark.parametrize("isdir, skipped", [(False, True), (True, False)])
    def test_probes_the_local_filesystem_outside_the_runner_container(self, isdir, skipped):
        env = _load_environment()
        sys.modules["steps.app_support"]._IN_CONTAINER = False
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG,), [_enabled_step(COPYOUS)]
        )
        with patch("os.path.isdir", return_value=isdir) as mock_isdir:
            assert env._skip_when_extension_not_installed(self._lts_context(), scenario) is skipped
        mock_isdir.assert_called_once_with(f"{env.SYSTEM_EXTENSION_DIR}/{COPYOUS}")

    def test_before_scenario_applies_the_gate(self):
        """The hook wiring: a tagged scenario on LTS skips before any sandbox work."""
        env = _load_environment()
        _probe(returncode=1)
        context = SimpleNamespace(
            is_bluefin_image=True,
            is_dakota_image=False,
            is_lts_image=True,
            failed_setup=None,
            optional_scenario_availability={},
            sandbox=None,
        )
        scenario = _scenario(
            (env.REQUIRES_INSTALLED_EXTENSION_TAG, "bluefin", "extensions"),
            [_enabled_step(COPYOUS)],
        )

        env.before_scenario(context, scenario)

        scenario.skip.assert_called_once()
        assert COPYOUS in scenario.skip.call_args[0][0]
