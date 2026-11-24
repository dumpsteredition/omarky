import QtQuick
import Quickshell

ShellRoot {
  Loader {
    id: pluginLoader
    active: true
    source: "__OMARKY_WIDGET_URL__"
    onLoaded: console.log("OMARKY_WIDGET_LOADED")
    onStatusChanged: {
      if (status === Loader.Error)
        console.warn("OMARKY_WIDGET_LOAD_ERROR", errorString())
    }
  }
}
