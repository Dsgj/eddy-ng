# This fork of eddy-ng is Klipper-only. The Kalico plugin entry point that
# previously lived here was removed with the Kalico import fallback
# (see CLAUDE.md, "Fork: Klipper-only, BTT Eddy-only").
def load_config_prefix(config):
    raise config.error(
        "eddy-ng: this fork is Klipper-only and cannot run as a Kalico plugin. "
        "Install into a Klipper tree with ./install.sh <klipper-dir>."
    )
