(ns meeting-notes.capture-agent.windows-test
  (:require [clojure.test :refer [deftest is testing]]
            [meeting-notes.capture-agent.windows :as windows]))

(deftest record-command-builds-required-args
  (let [argv (windows/record-command {:python "py"
                                      :script "scripts/windows_capture.py"
                                      :out-dir "recordings/demo"
                                      :seconds 3
                                      :speaker "Headphones"
                                      :microphone "USB"
                                      :sample-rate 44100
                                      :chunk-ms 50
                                      :all-devices? true})]
    (is (= "py" (first argv)))
    (is (= "record" (nth argv 2)))
    (is (some #{"--out-dir"} argv))
    (is (some #{"recordings/demo"} argv))
    (is (some #{"--seconds"} argv))
    (is (some #{"3"} argv))
    (is (some #{"Headphones"} argv))
    (is (some #{"USB"} argv))
    (is (some #{"44100"} argv))
    (is (some #{"50"} argv))
    (is (some #{"--all-devices"} argv))))

(deftest record-command-requires-out-dir
  (testing "out-dir is the only required capture target"
    (is (thrown-with-msg? clojure.lang.ExceptionInfo
                          #"requires :out-dir"
                          (windows/record-command {})))))
