"""
    minecraftinstall
"""
from __future__ import absolute_import, division, print_function
import hashlib
import re
import zipfile
from PySide import QtGui, QtCore
import logging
import os
from PySide.QtCore import Qt
from mcedit2.resourceloader import ResourceLoader
from mcedit2.ui.minecraft_installs import Ui_installsWidget
from mcedit2.util import settings
from mceditlib import directories

log = logging.getLogger(__name__)

installationsOption = settings.Settings().getOption("minecraft_installs/installations", "json", [])
multiMCInstallsOption = settings.Settings().getOption("minecraft_installs/multimc_installs", "json", [])
currentInstallOption = settings.Settings().getOption("minecraft_installs/current_install_path", unicode, "")
currentVersionOption = settings.Settings().getOption("minecraft_installs/current_version", unicode, "")
currentResourcePackOption = settings.Settings().getOption("minecraft_installs/current_resource_pack", unicode, "")
allowSnapshotsOption = settings.Settings().getOption("minecraft_installs/allow_snapshots_", bool, False)

_installs = None


def getResourceLoaderForFilename(filename):
    # Is this world inside a MultiMC instance?
    filename = os.path.normpath(filename)
    installs = GetInstalls()

    for instance in installs.instances:
        savesFolder = os.path.normpath(instance.saveFileDir)
        if filename.startswith(savesFolder):
            return instance.getResourceLoader()

    # Nope. Use the version and respack chosen in the world list.
    # ... should search for installs matching this one, but vanilla installs are still multi-version...
    loader = getSelectedResourceLoader()
    if not os.path.isdir(filename):
        worldFolder = os.path.dirname(filename)
    else:
        worldFolder = filename
    savesFolder = os.path.dirname(worldFolder)
    mcFolder = os.path.dirname(savesFolder)
    modsFolder = os.path.join(mcFolder, "mods")
    if os.path.isdir(modsFolder):
        log.info("Loading mods from %s", modsFolder)
        loader.addModsFolder(modsFolder)

    return loader


def getSelectedResourceLoader():
    install = GetInstalls().getCurrentInstall()
    if install is None:
        return GetInstalls().getDefaultResourceLoader()

    v = currentVersionOption.value()
    if not v:
        v = list(install.versions)[0]
    p = currentResourcePackOption.value() or None
    return install.getResourceLoader(v, p)


def GetInstalls():
    global _installs
    if _installs is None:
        _installs = MCInstallGroup()
    return _installs


def md5hash(filename):
    md5 = hashlib.md5()
    with file(filename, "rb") as f:
        md5.update(f.read())
        return md5.hexdigest()


class MCInstallGroup(object):
    def __init__(self):
        """
        Represents all Minecraft installs known to MCEdit. Loads installs from settings and detects the current install
        in ~/.minecraft or equivalent.

        Also represents MultiMC instances as separate installs, each with a single version and a single saves folder.

        :return:
        :rtype:
        """
        self._installations = list(self._loadInstalls())
        self._mmcInstalls = list(self._loadMMCInstalls())
        path = currentInstallOption.value()
        install = self.getInstall(path)
        if install is None:
            if len(self._installations):
                path = self._installations[0].path
                currentInstallOption.setValue(path)
            else:
                currentInstallOption.setValue("")

        self.getDefaultInstall()

    def _loadMMCInstalls(self):
        for install in multiMCInstallsOption.value():
            configFile = install["configFile"]
            try:
                install = MultiMCInstall(self, configFile)
                yield install
            except MultiMCInstallError as e:
                log.warn("Not using MultiMC with config file %s: %s", configFile, e)

    def _loadInstalls(self):
        for install in installationsOption.value():
            name = install["name"]
            path = install["path"]
            try:
                install = MCInstall(self, path, name)
                install.checkUsable()
                yield install
            except MCInstallError as e:
                log.warn("Not using install %s: %s", install.path, e)

    def _saveInstalls(self):
        installationsOption.setValue([i.getJsonSettingValue() for i in self._installations])
        multiMCInstallsOption.setValue([i.getJsonSettingValue() for i in self._mmcInstalls])
        log.info("MCInstall saved settings: %s", installationsOption.value())

    def getDefaultInstall(self):
        """
        Probes for a minecraft installation in the default install folder, and adds it to the group.

        :return:
        :rtype: MCInstall
        """
        minecraftDir = directories.minecraftDir
        defaultInstall = MCInstall(self, minecraftDir, "(Default)")
        try:
            defaultInstall.checkUsable()
        except MCInstallError as e:
            log.warn("Default install not usable: %s", e)
            return None
        else:
            value = defaultInstall.getJsonSettingValue()
            if value not in installationsOption.value():
                self._installations.append(defaultInstall)
                self._saveInstalls()
            if currentInstallOption.value() == "":
                currentInstallOption.setValue(defaultInstall.path)
            return defaultInstall

    def selectedInstallPath(self):
        path = currentInstallOption.value()
        for install in self._installations:
            if install.path == path:
                return path

        if len(self._installations):
            install = self._installations[0]
            currentInstallOption.setValue(install.path)
            return install.path

    @property
    def installs(self):
        return list(self._installations)

    def getInstall(self, path):
        for install in self._installations:
            if install.path == path:
                return install
        return None

    def getCurrentInstall(self):
        return self.getInstall(self.selectedInstallPath())

    def addInstall(self, install):
        self._installations.append(install)
        self._saveInstalls()

    def removeInstall(self, path):
        self._installations = [i for i in self._installations if i.path != path]
        self._saveInstalls()

    def addMMCInstall(self, install):
        self._mmcInstalls.append(install)
        self._saveInstalls()

    def removeMMCInstall(self, index):
        del self._mmcInstalls[index]
        self._saveInstalls()

    def ensureValidInstall(self):
        """
        Called on app startup. Display install config dialog if no installs were found

        :return:
        :rtype:
        """
        requiredVersion = self.findVersion1_9()
        if not requiredVersion:
            msgBox = QtGui.QMessageBox()
            msgBox.setWindowTitle("Minecraft not found.")
            msgBox.setText("MCEdit requires an installed Minecraft version 1.9 or greater to "
                           "access block textures, models, and metadata.")

            msgBox.exec_()
            installsWidget = MinecraftInstallsDialog()
            installsWidget.exec_()

    @property
    def mmcInstalls(self):
        return self._mmcInstalls

    @property
    def instances(self):
        for mmcInstall in self._mmcInstalls:
            for instance in mmcInstall.instances:
                yield instance

    def findVersion1_9(self):
        def matchVersion(version):
            major, minor, rev = splitVersion(version)
            if (major, minor) >= (1, 9):
                if rev == "":
                    return version
                try:
                    rev = int(rev[1:])  # skip revs like ".2-pre1" and ".1-OptiFine_HD_U_C7", only accept full releases
                    return version
                except ValueError:
                    return

        def jarOkay(path):
            return zipfile.is_zipfile(path)

        for install in self.installs:
            for v in install.versions:
                if matchVersion(v):
                    jarPath = install.getVersionJarPath(v)
                    if jarOkay(jarPath):
                        return jarPath

        for mmcInstall in self.mmcInstalls:
            for v in mmcInstall.versions:
                if matchVersion(v):
                    jarPath = mmcInstall.getVersionJarPath(v)
                    if jarOkay(jarPath):
                        return jarPath

    def getDefaultResourceLoader(self):
        v18 = self.findVersion1_9()
        loader = ResourceLoader(v18)
        loader.addZipFile(v18)
        return loader

class MCInstall(object):
    def __init__(self, installGroup, path, name="Unnamed"):
        self.installGroup = installGroup
        self.name = name
        self.path = path
        self.versionsDir = os.path.join(self.path, "versions")

    def checkUsable(self):
        """
        Raises MCInstallError explaining why this install is unusable, or else does nothing
        :return:
        :rtype:
        """
        log.info("Checking install at %s", self.path)
        if not os.path.exists(self.path):
            raise MCInstallError("Minecraft folder does not exist.")
        if not os.path.exists(self.versionsDir):
            raise MCInstallError("Minecraft versions folder does not exist.")
        if not len(self.versions):
            raise MCInstallError("Minecraft folder has no minecraft versions")
        log.info("Found versions:\n%s", self.versions)

    @property
    def versions(self):
        versions = os.listdir(self.versionsDir)
        return [v for v in versions
                if os.path.exists(self.getVersionJarPath(v))
                and (usableVersion(v) or allowSnapshotsOption.value())]

    def getVersionJarPath(self, version):
        return os.path.join(self.versionsDir, version, "%s.jar" % version)

    @property
    def resourcePacks(self):
        respackFolder = os.path.join(self.path, "resourcepacks")
        if not os.path.isdir(respackFolder):
            return []
        return os.listdir(respackFolder)

    def getSaveDirs(self):
        return [os.path.join(self.path, "saves")]  # xxx profile.json

    def getResourcePackPath(self, filename):
        return os.path.join(self.path, "resourcepacks", filename)

    def getJsonSettingValue(self):
        return {"name": self.name,
                "path": self.path}

    def getResourceLoader(self, version, resourcePack):
        v1_9 = self.installGroup.findVersion1_9()
        loader = ResourceLoader(v1_9)
        if resourcePack:
            try:
                loader.addZipFile(self.getResourcePackPath(resourcePack))
            except Exception as e:
                log.warn("Failed to load resource pack: %r\nPack: %s", e, resourcePack)
        path = self.getVersionJarPath(version)
        if not os.path.exists(path):
            if len(self.versions):
                log.warn("Version %s not found, falling back to first version found", version)
                path = self.getVersionJarPath(self.versions[0])
        loader.addZipFile(path)
        major, minor, rev = splitVersion(version)

        # Need v1.9 for multipart block models
        if (major, minor) != (1, 9):
            loader.addZipFile(v1_9)

        info = ["%s (%s)" % (z.filename, md5hash(z.filename)) for z in loader.zipFiles]
        log.info("Created ResourceLoader with search path:\n%s", ",\n".join(info))
        return loader


class MMCInstance(object):
    def __init__(self, install, path):
        instanceCfg = os.path.join(path, "instance.cfg")
        if not os.path.exists(instanceCfg):
            raise MultiMCInstanceError("instance.cfg not found: %s" % instanceCfg)

        instanceSettings = QtCore.QSettings(instanceCfg, QtCore.QSettings.IniFormat)

        self.version = instanceSettings.value("IntendedVersion", "")
        if not self.version:
            raise MultiMCInstanceError("Instance %s has no IntendedVersion" % os.path.basename(path))

        self.name = instanceSettings.value("name", "(unnamed)")
        self.install = install
        self.saveFileDir = os.path.join(path, "minecraft", "saves")
        self.modsDir = os.path.join(path, "minecraft", "mods")

    @property
    def versions(self):
        return [self.version]

    def getVersionJarPath(self):
        return self.install.getVersionJarPath(self.version)

    def getResourceLoader(self, resourcePack=None):
        v1_9 = self.install.installGroup.findVersion1_9()
        loader = ResourceLoader(v1_9)
        if resourcePack:
            loader.addZipFile(resourcePack)
        loader.addZipFile(self.getVersionJarPath())
        major, minor, rev = splitVersion(self.version)

        # Need v1.9 for multipart block models
        if (major, minor) != (1, 9):
            loader.addZipFile(v1_9)

        loader.addModsFolder(self.modsDir)

        info = ["%s (%s)" % (z.filename, md5hash(z.filename)) for z in loader.zipFiles]
        log.info("Created ResourceLoader with search path:\n%s", ",\n".join(info))
        return loader


class MultiMCInstall(object):
    def __init__(self, installGroup, configPath):
        self.installGroup = installGroup
        self.configPath = configPath
        if not os.path.exists(configPath):
            raise MultiMCInstallError("Config file does not exist", configPath)

        # MultiMC is built with Qt, so why not use Qt's settings loader?
        mmcSettings = QtCore.QSettings(configPath, QtCore.QSettings.IniFormat)
        instanceDir = mmcSettings.value("InstanceDir", "")
        if not instanceDir:
            raise MultiMCInstallError("InstanceDir not set")

        self.mmcDir = os.path.dirname(configPath)
        if not os.path.isabs(instanceDir):
            self.instanceDir = os.path.join(self.mmcDir, instanceDir)
        else:
            self.instanceDir = instanceDir

        self.versionsDir = os.path.join(self.mmcDir, "versions")

        self.name = os.path.basename(self.mmcDir)
        # read versions.dat? (qt binary json format)
        # read groups from instGroups.json?

    @property
    def instances(self):
        for filename in os.listdir(self.instanceDir):
            path = os.path.join(self.instanceDir, filename)
            if not os.path.isdir(path):
                continue

            try:
                instance = MMCInstance(self, path)
            except MultiMCInstanceError:
                log.error("Could not read MultiMC Instance")
                continue

            yield instance

    def getJsonSettingValue(self):
        return {"configFile": self.configPath}

    def getVersionJarPath(self, version):
        return os.path.join(self.versionsDir, version, version + ".jar")

    @property
    def versions(self):
        for version in os.listdir(self.versionsDir):
            if os.path.exists(os.path.join(self.versionsDir, version, version + ".jar")):
                yield version

def splitVersion(version):
    """
    Split a Minecraft version ID into major, minor, and revision. If the version could not be parsed, return (0, 0, "")
    The revision is returned with the leading period. For example, if "1.8.1-pre3" is passed, (1, 8, ".1-pre3") will
    be returned.

    :param version:
    :type version: unicode
    :return: major, minor, revision
    :rtype: int, int, unicode
    """
    try:
        match = re.search(r"(\d+)\.(\d+)(.*)", version)
        if match is None:
            return 0, 0, ""
        groups = match.groups()
        if len(groups) < 2:
            return 0, 0, ""
        if len(groups) < 3:
            return int(groups[0]), int(groups[1]), ""
        return int(groups[0]), int(groups[1]), groups[2]
    except ValueError:
        return 0, 0, ""

def usableVersion(version):
    """
    Return True if the version has unstitched texture data in assets/minecraft/textures/blocks (v1.6+)
    (earlier versions have unstitched textures elsewhere, so don't bother with them)
    :param version:
    :type version:
    :return:
    :rtype:
    """
    major, minor, rev = splitVersion(version)
    if major < 1:
        return False
    if minor < 6:
        return False
    return True


class MCInstallError(ValueError):
    """
    Raised for invalid or unusable Minecraft installs.
    """


class MultiMCInstallError(ValueError):
    """
    Raised for invalid or unreadable MultiMC installs.
    """


class MultiMCInstanceError(ValueError):
    """
    Raised for invalid or unreadable MultiMC instances.
    """


class NameItem(QtGui.QTableWidgetItem):
    def setData(self, data, role):
        if role != Qt.EditRole or data != "(Default)":
            super(NameItem, self).setData(data, role)


class PathItem(QtGui.QTableWidgetItem):
    def setData(self, data, role):
        if role != Qt.EditRole or os.path.exists(data):
            super(PathItem, self).setData(data, role)


class MinecraftInstallsDialog(QtGui.QDialog, Ui_installsWidget):
    def __init__(self, *args, **kwargs):
        super(MinecraftInstallsDialog, self).__init__(*args, **kwargs)
        self.setupUi(self)


        # populate list view
        path = currentInstallOption.value()
        for i, install in enumerate(GetInstalls().installs):
            self._addInstall(install)
            if path == install.path:
                self._hiliteRow(i)

        for path in GetInstalls().mmcInstalls:
            self._addMMCInstall(path)

        self.minecraftInstallsTable.cellChanged.connect(self.itemChanged)
        self.addButton.clicked.connect(self.addInstall)
        self.removeButton.clicked.connect(self.removeInstall)
        self.selectButton.clicked.connect(self.selectInstall)
        self.okButton.clicked.connect(self.ok)

        self.snapshotsCheckBox.setChecked(allowSnapshotsOption.value())
        self.snapshotsCheckBox.toggled.connect(allowSnapshotsOption.setValue)
        allowSnapshotsOption.valueChanged.connect(self.allowSnapshotsChanged)

        self.addMMCButton.clicked.connect(self.addMMCInstall)
        self.removeMMCButton.clicked.connect(self.removeMMCInstall)

    def itemChanged(self, row, column):
        install = GetInstalls().installs[row]
        text = self.minecraftInstallsTable.item(row, column).text()
        if column == 0:
            install.name = text
        if column == 2:
            install.path = text  # xxxx validate me!

    def allowSnapshotsChanged(self):
        installs = GetInstalls().installs
        for row in range(self.minecraftInstallsTable.rowCount()):
            install = installs[row]
            versionsString = ", ".join(sorted(install.versions, reverse=True))
            versionsItem = self.minecraftInstallsTable.item(row, 1)
            versionsItem.setText(versionsString)

    def _addInstall(self, install):
        minecraftInstallsTable = self.minecraftInstallsTable
        row = minecraftInstallsTable.rowCount()
        minecraftInstallsTable.setRowCount(row+1)
        nameItem = NameItem(install.name)
        if install.name == "(Default)":
            nameItem.setFlags(nameItem.flags() & ~Qt.ItemIsEditable)
        minecraftInstallsTable.setItem(row, 0, nameItem)

        versionsString = ", ".join(sorted(install.versions, reverse=True))
        versionsItem = QtGui.QTableWidgetItem(versionsString)
        versionsItem.setFlags(versionsItem.flags() & ~Qt.ItemIsEditable)
        self.minecraftInstallsTable.setItem(row, 1, versionsItem)

        pathItem = PathItem(install.path)
        if install.name == "(Default)":
            pathItem.setFlags(pathItem.flags() & ~Qt.ItemIsEditable)
        minecraftInstallsTable.setItem(row, 2, pathItem)
        self._hiliteRow(row)
        currentInstallOption.setValue(install.path)

    def _addMMCInstall(self, install):
        mmcTable = self.multiMCTable
        row = mmcTable.rowCount()
        mmcTable.setRowCount(row + 1)
        nameItem = NameItem(install.name)
        nameItem.setFlags(nameItem.flags() & ~Qt.ItemIsEditable)
        mmcTable.setItem(row, 0, nameItem)

        instancesString = ", ".join(sorted((i.name for i in install.instances)))
        instancesItem = QtGui.QTableWidgetItem(instancesString)
        instancesItem.setFlags(instancesItem.flags() & ~Qt.ItemIsEditable)
        mmcTable.setItem(row, 1, instancesItem)

        pathItem = PathItem(install.configPath)
        mmcTable.setItem(row, 2, pathItem)

    def _hiliteRow(self, hiliteRow):
        for row in range(self.minecraftInstallsTable.rowCount()):
            for column in range(self.minecraftInstallsTable.columnCount()):
                item = self.minecraftInstallsTable.item(row, column)
                font = item.font()
                font.setBold(row == hiliteRow)
                item.setFont(font)

    def addInstall(self):
        folder = QtGui.QFileDialog.getExistingDirectory(self, "Choose a Minecraft installation folder (.minecraft)")
        installs = GetInstalls()
        if not folder:
            return
        try:
            install = MCInstall(installs, folder)
            install.checkUsable()
        except MCInstallError as e:
            message = "This minecraft install is unusable.\n(%s)" % e.message
            QtGui.QMessageBox.warning(self, "Minecraft Install Unusable", message)
        else:
            installs.addInstall(install)
            self._addInstall(install)

    def removeInstall(self):
        row = self.minecraftInstallsTable.currentRow()
        path = self.minecraftInstallsTable.item(row, 2).data()
        GetInstalls().removeInstall(path)
        self.minecraftInstallsTable.removeRow(row)

    def selectInstall(self):
        row = self.minecraftInstallsTable.currentRow()
        path = self.minecraftInstallsTable.item(row, 2).data()
        currentInstallOption.setValue(path)
        self._hiliteRow(row)

    def addMMCInstall(self):
        result = QtGui.QFileDialog.getOpenFileName(self,
                                                   "Choose a MultiMC configuration file (multimc.cfg)",
                                                   filter="MultiMC configuration files (multimc.cfg)")
        installs = GetInstalls()
        if not result:
            return
        configPath = result[0]
        if not configPath:
            return

        try:
            install = MultiMCInstall(installs, configPath)
        except MultiMCInstallError as e:
            message = "This MultiMC install is unusable.\n(%s)" % e.message
            QtGui.QMessageBox.warning(self, "MultiMC Install Unusable", message)
        else:
            installs.addMMCInstall(install)
            self._addMMCInstall(install)

    def removeMMCInstall(self):
        row = self.multiMCTable.currentRow()
        GetInstalls().removeMMCInstall(row)
        self.multiMCTable.removeRow(row)

    def ok(self):
        self.close()

    def closeEvent(self, event):
        if not self.close():
            event.ignore()

    def close(self):
        if not GetInstalls().findVersion1_9():
            button = QtGui.QMessageBox.critical(self,
                                                "Minecraft Install Needed",
                                                "Cannot start MCEdit without at least one Minecraft installation version "
                                                "1.9 or greater.",
                                                QtGui.QMessageBox.Close | QtGui.QMessageBox.Cancel,
                                                QtGui.QMessageBox.Cancel)

            if button == QtGui.QMessageBox.Close:
                raise SystemExit
            return False
        else:
            super(MinecraftInstallsDialog, self).close()
            return True
