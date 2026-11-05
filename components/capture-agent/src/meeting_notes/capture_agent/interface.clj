(ns meeting-notes.capture-agent.interface
  "Public API for launching local capture helpers."
  (:require [meeting-notes.capture-agent.windows :as windows]))

(defn windows-script-path
  "Return the default path to the Windows Python capture helper."
  []
  (windows/default-script-path))

(defn list-windows-devices
  "List Windows audio devices through the helper.

  Options:
  - `:python` path or command for Windows Python, defaults to `python`
  - `:script` path to `scripts/windows_capture.py`, defaults to repo helper"
  ([] (list-windows-devices {}))
  ([opts]
   (windows/list-devices opts)))

(defn windows-record-command
  "Build the command vector used to start a Windows recording.

  Pure helper for tests and callers that want to supervise the process
  themselves."
  [opts]
  (windows/record-command opts))

(defn start-windows-recording!
  "Start recording Windows system loopback + mic.

  Required:
  - `:out-dir` output directory

  Optional:
  - `:seconds` duration in seconds; omit for indefinite recording
  - `:speaker` speaker display-name substring for loopback source
  - `:microphone` microphone display-name substring
  - `:all-devices?` record every speaker loopback and every working mic
  - `:sample-rate` default 48000
  - `:chunk-ms` default 100
  - `:python` default `python.exe`
  - `:script` default repo helper"
  [opts]
  (windows/start-recording! opts))

(defn stop-recording!
  "Stop a process returned by `start-windows-recording!`."
  [capture]
  (windows/stop-recording! capture))
