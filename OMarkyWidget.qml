import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The mark opens OMarky on a deliberate click; nothing runs during shell load.
BarWidget {
  id: root
  moduleName: "io.github.dumpsteredition.omarky"

  property bool shortcutCopyAttempted: false
  property bool shortcutCopySucceeded: false

  readonly property string appPath: decodeURIComponent(
    String(Qt.resolvedUrl("app/omarky.py")).replace(/^file:\/\//, ""))
  readonly property string shortcutHelperPath: decodeURIComponent(
    String(Qt.resolvedUrl("bin/omarky-shortcut")).replace(/^file:\/\//, ""))
  readonly property string launchTooltip: !shortcutCopyAttempted
    ? strings.t("bar.launch.tooltip")
    : (shortcutCopySucceeded
      ? strings.t("bar.shortcut.copied")
      : strings.t("bar.shortcut.failed"))

  implicitWidth: buttons.implicitWidth
  implicitHeight: buttons.implicitHeight

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  function startApp(action) {
    if (appProcess.running) return
    appProcess.command = ["/usr/bin/python3", root.appPath, action]
    appProcess.running = true
  }

  function copyShortcut() {
    if (shortcutProcess.running) return
    shortcutCopyAttempted = true
    shortcutCopySucceeded = false
    shortcutProcess.running = true
  }

  function showShortcutFeedback() {
    if (root.bar && typeof root.bar.showTooltip === "function")
      root.bar.showTooltip(primaryButton, root.launchTooltip)
  }

  Row {
    id: buttons
    spacing: 0
    anchors.verticalCenter: parent.verticalCenter

    BarIconButton {
      id: primaryButton
      bar: root.bar
      slotSize: Style.bar.iconSlot
      text: ""
      iconComponent: Component {
        Item {
          width: 16
          height: 16
          implicitWidth: 16
          implicitHeight: 16

          Rectangle {
            width: 10
            height: 10
            radius: 5
            anchors.centerIn: parent
            color: "transparent"
            border.width: 1.5
            border.color: primaryButton.foreground
          }

          Rectangle {
            width: 2.5
            height: 2.5
            radius: 1.25
            anchors.centerIn: parent
            color: Commons.Color.accent
          }
        }
      }
      tooltipText: root.launchTooltip
      Accessible.role: Accessible.Button
      Accessible.name: strings.t("bar.launch.name")
      Accessible.description: strings.t("bar.launch.description")
      onPressed: function(button) {
        if (button === Qt.LeftButton) root.startApp("launch")
        else if (button === Qt.RightButton) root.copyShortcut()
      }
    }

    BarIconButton {
      id: setupButton
      bar: root.bar
      slotSize: Style.bar.iconSlot
      text: "⚙"
      fontFamily: Style.font.family
      tooltipText: strings.t("bar.setup.tooltip")
      Accessible.role: Accessible.Button
      Accessible.name: strings.t("bar.setup.name")
      Accessible.description: strings.t("bar.setup.description")
      onPressed: function(button) {
        if (button === Qt.LeftButton) root.startApp("setup")
      }
    }
  }

  Process {
    id: appProcess
    running: false
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
  }

  Process {
    id: shortcutProcess
    command: ["/usr/bin/python3", root.shortcutHelperPath, "--copy"]
    running: false
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      root.shortcutCopySucceeded = exitCode === 0
      Qt.callLater(root.showShortcutFeedback)
    }
  }
}
