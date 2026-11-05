(ns meeting-notes.capture-agent.cli
  (:require [clojure.pprint :as pprint]
            [meeting-notes.capture-agent.interface :as capture]))

(defn- usage []
  (binding [*out* *err*]
    (println "Usage:")
    (println "  clojure -M:dev -m meeting-notes.capture-agent.cli list-devices")
    (println "  clojure -M:dev -m meeting-notes.capture-agent.cli record-command <out-dir> [seconds] [--all-devices]")))

(defn -main
  [& args]
  (try
    (case (first args)
      "list-devices"
      (pprint/pprint (capture/list-windows-devices))

      "record-command"
      (let [[_ out-dir & more] args
            all-devices? (some #{"--all-devices"} more)
            seconds (first (remove #{"--all-devices"} more))]
        (if out-dir
          (pprint/pprint
           (capture/windows-record-command
            (cond-> {:out-dir out-dir}
              seconds (assoc :seconds seconds)
              all-devices? (assoc :all-devices? true))))
          (do
            (usage)
            (System/exit 2))))

      (do
        (usage)
        (System/exit 2)))
    (finally
      (shutdown-agents))))
