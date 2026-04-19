import os
import platform
import shutil
import json
import re
import time
from datetime import datetime
#from urllib.request import urlopen
from six.moves import urllib

from enigma import eConsoleAppContainer, eDVBDB, eTimer

from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Button import Button
from Tools.Directories import resolveFilename, SCOPE_PLUGINS
from Plugins.Plugin import PluginDescriptor


PLUGIN_VERSION = "2.2"
PLUGIN_NAME = "CiefpSettingsT2miAbertis"
ICON_PATH = "/usr/lib/enigma2/python/Plugins/Extensions/CiefpSettingsT2miAbertis/icon.png"

# Motor settings ZIPs repo (multiple zips exist; we pick the newest date for the chosen prefix)
GITHUB_ZIPPED_ROOT_API = "https://api.github.com/repos/ciefp/ciefpsettings-enigma2-zipped/contents/"
MOTOR_ZIP_PATTERN = re.compile(r"^ciefp-E2-75E-34W-(\d{2}\.\d{2}\.\d{4})\.zip$", re.IGNORECASE)

LOG_FILE = "/var/log/ciefp_installer.log"


class CiefpSettingsT2miAbertis(Screen):
    skin = """
    <screen name="CiefpSettingsT2miAbertis" position="center,center" size="1600,800" title="CiefpSettings T2mi Abertis Installer (v{version})">
        <widget name="info" position="10,10" size="780,650" font="Regular;24" valign="center" halign="left" />
        <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/CiefpSettingsT2miAbertis/background.png" position="790,10" size="800,650" alphatest="on" />

        <widget name="status" position="10,670" size="1580,50" font="Bold;24" valign="center" halign="center" backgroundColor="#cccccc" foregroundColor="#000000" />

        <widget name="key_red" position="10,730" size="370,60" font="Bold;26" halign="center" backgroundColor="#9F1313" foregroundColor="#000000" />
        <widget name="key_green" position="410,730" size="370,60" font="Bold;26" halign="center" backgroundColor="#1F771F" foregroundColor="#000000" />
        <widget name="key_yellow" position="810,730" size="370,60" font="Bold;26" halign="center" backgroundColor="#D6A200" foregroundColor="#000000" />
        <widget name="key_blue" position="1210,730" size="380,60" font="Bold;26" halign="center" backgroundColor="#1E5AA8" foregroundColor="#000000" />
    </screen>
    """.format(version=PLUGIN_VERSION)

    def __init__(self, session):
        self.session = session
        Screen.__init__(self, session)

        self._container = None
        self._on_cmd_done = None

        # Install timing + state
        self._install_start_time = 0.0
        self._astra_preinstalled = None
        self._copy_attempt = 0
        self._max_copy_attempts = 2
        self._last_motor_version = None

        # Retry timer for copy step
        self._retry_timer = eTimer()
        try:
            self._retry_timer_conn = self._retry_timer.timeout.connect(self._retryCopyNow)
        except Exception:
            self._retry_timer.callback.append(self._retryCopyNow)

        self.setupUI()
        self.showPrompt()

    def setupUI(self):
        self["info"] = Label("Initializing plugin...")
        self["status"] = Label("")
        self["key_red"] = Button("Exit")
        self["key_green"] = Button("Install")
        self["key_yellow"] = Button("Update")
        self["key_blue"] = Button("Motor Settings\n(ciefp-E2-75E-34W)")

        self["actions"] = ActionMap(["ColorActions", "SetupActions"], {
            "red": self.exitPlugin,
            "green": self.startInstallation,
            "yellow": self.runUpdate,
            "blue": self.installMotorSettings,
            "cancel": self.close
        }, -1)

    def showPrompt(self):
        self["info"].setText(
            "GREEN (Install):\n"
            "- Check if Astra-SM is already installed\n"
            "- Stop Astra-SM, copy config/scripts, start Astra-SM\n"
            "- Auto retry on clean install (30s)\n\n"
            "BLUE (Motor Settings):\n"
            "- Download latest 'ciefp-E2-75E-34W' ZIP from GitHub\n"
            "- Install to /etc/enigma2 (+ satellites.xml if present)\n"
            "- Reload servicelist & bouquets\n\n"
            "YELLOW (Update):\n"
            "- Update installer script\n\n"
            "Log file: %s" % LOG_FILE
        )
        self["status"].setText("Awaiting your choice.")

    # -------------------------
    # Helpers (logging + info)
    # -------------------------
    def _write_log(self, message):
        try:
            line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
            print("[CiefpInstaller] " + message)
            with open(LOG_FILE, "a") as f:
                f.write(line)
        except Exception:
            pass

    def _get_image_version(self):
        for path in ("/etc/image-version", "/etc/openatv-version", "/etc/issue"):
            try:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        txt = f.read().strip()
                    if txt:
                        return (txt[:120] + "...") if len(txt) > 120 else txt
            except Exception:
                continue
        return "unknown"

    def _format_elapsed(self, start_time):
        if not start_time:
            return "unknown"
        elapsed = time.time() - start_time
        return "<1s" if elapsed < 1.0 else "%ds" % int(elapsed)

    # -------------------------
    # Non-blocking shell runner
    # -------------------------
    def runCommandAsync(self, command, done_cb=None, status_text=None):
        if status_text:
            self["status"].setText(status_text)

        if self._container is not None:
            self["status"].setText("Busy, please wait...")
            return

        self._on_cmd_done = done_cb
        self._container = eConsoleAppContainer()
        self._container.appClosed.append(self._commandFinished)
        try:
            self._container.execute(command)
        except Exception as e:
            self._container = None
            self._on_cmd_done = None
            self["status"].setText("Command start failed: %s" % str(e))

    def _commandFinished(self, retval):
        cb = self._on_cmd_done
        self._container = None
        self._on_cmd_done = None
        if cb:
            try:
                cb(retval)
            except Exception as e:
                self["status"].setText("Callback error: %s" % str(e))

    # -------------------------
    # Safe copy for executables
    # -------------------------
    def safe_copy_executable(self, src, dest, mode=0o755):
        tmp = dest + ".new"
        shutil.copy2(src, tmp)
        os.chmod(tmp, mode)
        os.rename(tmp, dest)

    def _stop_astra_cmd(self):
        return (
            "if [ -x /etc/init.d/astra-sm ]; then /etc/init.d/astra-sm stop >/dev/null 2>&1; fi; "
            "killall -9 astra-sm >/dev/null 2>&1; "
        )

    def _start_astra_cmd(self):
        return "if [ -x /etc/init.d/astra-sm ]; then /etc/init.d/astra-sm start >/dev/null 2>&1; fi;"

    # -----------------
    # UPDATE (yellow)
    # -----------------
    def runUpdate(self):
        cmd = 'wget -q "--no-check-certificate" https://raw.githubusercontent.com/ciefp/CiefpSettingsT2miAbertis/main/installer.sh -O - | /bin/sh'
        self.runCommandAsync(cmd, done_cb=self._updateDone, status_text="Updating plugin...")

    def _updateDone(self, retval):
        self["status"].setText("Update complete." if retval == 0 else "Update failed (code %d)." % retval)

    # -----------------
    # INSTALL (green)
    # -----------------
    def startInstallation(self):
        system_info = platform.machine()
        if system_info not in ["mips", "arm", "armv7", "armv7l"]:
            self["status"].setText("Unsupported architecture: %s" % system_info)
            return

        self._install_start_time = time.time()
        self._copy_attempt = 0
        self._astra_preinstalled = None

        self["info"].setText("Checking if Astra-SM is already installed...")
        self.runCommandAsync(
            "opkg list-installed 2>/dev/null | grep -qi '^astra-sm '",
            done_cb=self._astraCheckDone,
            status_text="Checking Astra-SM..."
        )

    def _astraCheckDone(self, retval):
        if retval == 0:
            self._astra_preinstalled = True
            self["info"].setText("Astra-SM already installed. Proceeding...")
            self._write_log("Astra-SM already installed -> skipping opkg install")
            self._astraInstalledStopForCopy(0)
            return

        self._astra_preinstalled = False
        self["info"].setText("Installing Astra-SM (opkg)...")
        self._write_log("Astra-SM not installed -> running: opkg install astra-sm")
        self.runCommandAsync(
            "opkg install astra-sm",
            done_cb=self._astraInstalledStopForCopy,
            status_text="Installing Astra-SM (opkg)..."
        )

    def _astraInstalledStopForCopy(self, retval):
        self["info"].setText("Stopping Astra-SM to copy files safely...")
        self.runCommandAsync(self._stop_astra_cmd(), done_cb=self._copyPluginFiles, status_text="Stopping Astra-SM...")

    def _copyPluginFiles(self, retval):
        self._copy_attempt += 1
        try:
            self["info"].setText("Copying configuration files... (attempt %d/%d)" % (self._copy_attempt, self._max_copy_attempts))
            self["status"].setText("Copying files...")

            for d in ["/etc/astra", "/etc/astra/scripts", "/etc/tuxbox/config", "/etc/tuxbox/config/oscam-emu"]:
                os.makedirs(d, exist_ok=True)

            data_dir = resolveFilename(SCOPE_PLUGINS, "Extensions/CiefpSettingsT2miAbertis/data/")
            shutil.copy2(os.path.join(data_dir, "sysctl.conf"), "/etc/sysctl.conf")
            shutil.copy2(os.path.join(data_dir, "astra.conf"), "/etc/astra/astra.conf")

            system_info = platform.machine()
            script_arch = "arm" if system_info in ["arm", "armv7", "armv7l"] else "mips" if system_info == "mips" else None
            if not script_arch:
                raise Exception("Unsupported architecture: %s" % system_info)

            self.safe_copy_executable(os.path.join(data_dir, script_arch, "abertis"), "/etc/astra/scripts/abertis", mode=0o755)

            src_softcam = os.path.join(data_dir, "SoftCam.Key")
            shutil.copy2(src_softcam, "/etc/tuxbox/config/softcam.key")
            shutil.copy2(src_softcam, "/etc/tuxbox/config/oscam-emu/softcam.key")

        except Exception as e:
            if self._copy_attempt < self._max_copy_attempts:
                self._write_log("Copy failed on attempt %d: %s -> retry in 30s" % (self._copy_attempt, str(e)))
                self["status"].setText("Copy failed: %s. Retrying in 30s..." % str(e))
                self["info"].setText("Waiting 30 seconds then retry copy (attempt %d/%d)..." % (self._copy_attempt + 1, self._max_copy_attempts))
                try:
                    self._retry_timer.start(30000, True)
                except Exception:
                    self._retry_timer.start(30000, 1)
                return

            self._write_log("Copy failed final: %s" % str(e))
            self["status"].setText("Copy error: %s" % str(e))
            self.runCommandAsync(self._start_astra_cmd(), status_text="Starting Astra-SM...")
            return

        self["info"].setText("Starting Astra-SM...")
        self.runCommandAsync(self._start_astra_cmd(), done_cb=self._installFinish, status_text="Starting Astra-SM...")

    def _retryCopyNow(self):
        self["status"].setText("Retrying copy...")
        self.runCommandAsync(self._stop_astra_cmd(), done_cb=self._copyPluginFiles, status_text="Stopping Astra-SM (retry)...")

    def _installFinish(self, retval):
        time_string = self._format_elapsed(self._install_start_time)

        attempt_info = "SUCCESS on first attempt" if self._copy_attempt == 1 else "SUCCESS after retry (attempt %d)" % self._copy_attempt
        astra_info = "astra-sm preinstalled" if self._astra_preinstalled else "astra-sm installed now"
        image_ver = self._get_image_version()

        self._write_log("%s | Time: %s | %s | Image: %s" % (attempt_info, time_string, astra_info, image_ver))

        self["status"].setText("Install done in %s. Press BLUE for Motor Settings." % time_string)
        self["info"].setText(
            "Installation successful!\n\n"
            "%s\n"
            "Time: %s\n"
            "%s\n\n"
            "Next step:\n"
            "- Press BLUE (Motor Settings)\n\n"
            "Image: %s" % (attempt_info, time_string, astra_info, image_ver)
        )

        self.session.open(
            MessageBox,
            "Files copied and Astra-SM restarted.\n\n%s\nTime: %s\n\nPress BLUE for Motor Settings." % (attempt_info, time_string),
            MessageBox.TYPE_INFO
        )

    # -------------------------
    # MOTOR SETTINGS (blue)
    # -------------------------
    def _pick_latest_motor_zip(self, items):
        best_url = None
        best_dt = None
        for it in items:
            name = it.get("name", "")
            m = MOTOR_ZIP_PATTERN.match(name)
            if not m:
                continue

            date_str = m.group(1)
            try:
                dt = datetime.strptime(date_str, "%d.%m.%Y")
            except Exception:
                dt = None

            url = it.get("download_url")
            if not url:
                continue

            if best_url is None:
                best_url, best_dt = url, dt
                continue

            if dt and best_dt:
                if dt > best_dt:
                    best_url, best_dt = url, dt
            elif dt and not best_dt:
                best_url, best_dt = url, dt

        return best_url, best_dt

    def getLatestMotorZipUrl(self):
        try:
            resp = urlopen(GITHUB_ZIPPED_ROOT_API, timeout=20)
            items = json.loads(resp.read().decode("utf-8"))
            return self._pick_latest_motor_zip(items)
        except Exception as e:
            self["status"].setText("GitHub fetch error: %s" % str(e))
            self._write_log("GitHub fetch error (motor): %s" % str(e))
            return None, None

    def installMotorSettings(self):
        self["status"].setText("Checking latest Motor Settings ZIP...")
        zip_url, zip_dt = self.getLatestMotorZipUrl()
        if not zip_url:
            self["status"].setText("No matching motor ZIP found on GitHub.")
            return

        shown_ver = zip_dt.strftime("%d.%m.%Y") if zip_dt else "unknown"
        self._last_motor_version = shown_ver
        self._write_log("Motor Settings: selected version %s | %s" % (shown_ver, zip_url))

        self["info"].setText("Installing Motor Settings...\n\nFound version: %s\nSource:\n%s" % (shown_ver, zip_url))

        cmd = (
            "opkg install unzip >/dev/null 2>&1; "
            "rm -rf /tmp/ciefp_motor /tmp/ciefp_motor.zip; "
            "mkdir -p /tmp/ciefp_motor; "
            "wget -O /tmp/ciefp_motor.zip \"%s\" >/dev/null 2>&1; "
            "unzip -o /tmp/ciefp_motor.zip -d /tmp/ciefp_motor >/dev/null 2>&1; "
            "cp -rf /tmp/ciefp_motor/*/* /etc/enigma2/ >/dev/null 2>&1; "
            "if [ -f /tmp/ciefp_motor/*/satellites.xml ]; then "
            "  mkdir -p /etc/tuxbox/; "
            "  cp -f /tmp/ciefp_motor/*/satellites.xml /etc/tuxbox/ >/dev/null 2>&1; "
            "fi; "
            "sync; "
            "rm -rf /tmp/ciefp_motor /tmp/ciefp_motor.zip; "
        ) % zip_url

        self.runCommandAsync(cmd, done_cb=self._motorSettingsDone, status_text="Installing Motor Settings...")

    def _motorSettingsDone(self, retval):
        if retval != 0:
            self["status"].setText("Motor Settings install failed (code %d)." % retval)
            self._write_log("Motor Settings install failed (code %d)" % retval)
            return

        try:
            db = eDVBDB.getInstance()
            db.reloadServicelist()
            db.reloadBouquets()
            self["status"].setText("Motor Settings installed & reloaded successfully.")
            self._write_log("Motor Settings installed OK | version %s" % (self._last_motor_version or "unknown"))
        except Exception as e:
            self["status"].setText("Installed, but reload failed: %s" % str(e))
            self._write_log("Motor Settings reload failed: %s" % str(e))

    def exitPlugin(self):
        self.close()


def Plugins(**kwargs):
    return [
        PluginDescriptor(
            name=PLUGIN_NAME,
            description="Installer for T2MI Abertis configuration (Version %s)" % PLUGIN_VERSION,
            where=[PluginDescriptor.WHERE_PLUGINMENU, PluginDescriptor.WHERE_EXTENSIONSMENU],
            icon=ICON_PATH,
            fnc=lambda session, **kwargs: session.open(CiefpSettingsT2miAbertis)
        )
    ]
