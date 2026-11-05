(ns meeting-notes.capture-agent.windows
  "Windows WASAPI helper launcher.

  This namespace intentionally treats Windows audio capture as an external
  process. WSL orchestrates; Windows Python opens WASAPI devices."
  (:require [clojure.data.json :as json]
            [clojure.java.io :as io]
            [clojure.java.shell :as shell]
            [clojure.string :as str])
  (:import (java.io File)
           (java.lang ProcessBuilder ProcessBuilder$Redirect)
           (java.util.concurrent TimeUnit)))

(def ^:private default-python "python.exe")
(def ^:private default-sample-rate 48000)
(def ^:private default-chunk-ms 100)

(defn default-script-path
  "Return the repo-local helper path as an absolute Linux path."
  []
  (.getAbsolutePath (io/file "scripts" "windows_capture.py")))

(defn- windows-path
  "Convert a WSL/Linux path to a Windows path when `wslpath` is available.
  Plain Windows-looking paths are returned unchanged."
  [path]
  (let [s (str path)]
    (cond
      (re-find #"^[A-Za-z]:[\\/]" s) s
      (not (.startsWith s "/")) s
      :else
      (let [{:keys [exit out]} (shell/sh "wslpath" "-w" s)]
        (if (zero? exit)
          (str/trim out)
          s)))))

(defn- command-prefix
  [{:keys [python script]
    :or {python default-python}}]
  [python (windows-path (or script (default-script-path)))])

(defn list-command
  "Build argv for listing Windows audio devices."
  [opts]
  (conj (command-prefix opts) "list-devices"))

(defn record-command
  "Build argv for the Windows capture helper."
  [{:keys [out-dir seconds speaker microphone sample-rate chunk-ms all-devices?] :as opts}]
  (when-not (seq (str out-dir))
    (throw (ex-info "record-command requires :out-dir" {:opts opts})))
  (cond-> (into (command-prefix opts)
                ["record"
                 "--out-dir" (windows-path out-dir)
                 "--sample-rate" (str (or sample-rate default-sample-rate))
                 "--chunk-ms" (str (or chunk-ms default-chunk-ms))])
    seconds
    (into ["--seconds" (str seconds)])

    speaker
    (into ["--speaker" speaker])

    microphone
    (into ["--microphone" microphone])

    all-devices?
    (conj "--all-devices")))

(defn- run-json!
  [argv]
  (let [{:keys [exit out err]} (apply shell/sh argv)]
    (when-not (zero? exit)
      (throw (ex-info "Windows capture helper failed"
                      {:argv argv :exit exit :stdout out :stderr err})))
    (json/read-str out :key-fn keyword)))

(defn list-devices
  "Return helper-reported device data as Clojure maps."
  [opts]
  (run-json! (list-command opts)))

(defn start-recording!
  "Spawn the Windows capture helper. Returns a capture map with process and argv."
  [{:keys [out-dir log-file] :as opts}]
  (let [argv (record-command opts)
        log  (io/file (or log-file (str out-dir File/separator "capture.log")))
        _    (when-let [parent (.getParentFile log)]
               (.mkdirs parent))
        pb   (doto (ProcessBuilder. ^java.util.List argv)
               (.redirectOutput (ProcessBuilder$Redirect/appendTo log))
               (.redirectErrorStream true))
        proc (.start pb)]
    {:process proc
     :argv argv
     :out-dir out-dir
     :log-file (.getAbsolutePath log)
     :started-at (System/currentTimeMillis)}))

(defn stop-recording!
  "Terminate a running capture process."
  [{:keys [^Process process] :as capture}]
  (when process
    (.destroy process)
    (when-not (.waitFor process 5 TimeUnit/SECONDS)
      (.destroyForcibly process)
      (.waitFor process 5 TimeUnit/SECONDS)))
  (assoc capture
         :stopped-at (System/currentTimeMillis)
         :exit (when process (.exitValue process))))
