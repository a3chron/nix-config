# Watches BlueZ over D-Bus; starts/stops the Horus voice service when the
# Nothing headphones (dis)connect. MAC is substituted by voice.nix via env.
import os
import subprocess

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

MAC = os.environ.get("HORUS_HEADPHONE_MAC", "3C:B0:ED:A7:8B:42")
PATH_SUFFIX = "dev_" + MAC.replace(":", "_")


def set_voice(active):
    action = "start" if active else "stop"
    subprocess.run(["systemctl", "--user", action, "horus-voice.service"], check=False)
    print(f"voice {action}", flush=True)
    if active:
        # picking up the headphones = about to talk — warm the model now so the
        # first request isn't a cold ~40-50s hit. horus-warmup gates itself
        # (skips if a GPU-heavy app runs, the stack is paused, or it's already
        # loaded), so fire-and-forget is safe.
        try:
            subprocess.Popen(
                ["horus-warmup"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print("warmup dispatched", flush=True)
        except OSError as e:
            print(f"warmup dispatch failed: {e}", flush=True)


def props_changed(iface, changed, invalidated, path=None):
    if iface != "org.bluez.Device1" or "Connected" not in changed:
        return
    if path and path.endswith(PATH_SUFFIX):
        set_voice(bool(changed["Connected"]))


DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
bus.add_signal_receiver(
    props_changed,
    dbus_interface="org.freedesktop.DBus.Properties",
    signal_name="PropertiesChanged",
    path_keyword="path",
)

try:
    obj = bus.get_object("org.bluez", f"/org/bluez/hci0/{PATH_SUFFIX}")
    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
    set_voice(bool(props.Get("org.bluez.Device1", "Connected")))
except dbus.exceptions.DBusException:
    pass

GLib.MainLoop().run()
