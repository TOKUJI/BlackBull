# Shared tester identity for every Autobahn entry point. A digest bump must
# move the runner and the CaseSet resolver together so they cannot disagree
# about which concrete case IDs a selector names.
AUTOBAHN_IMAGE="${AUTOBAHN_IMAGE:-crossbario/autobahn-testsuite@sha256:519915fb568b04c9383f70a1c405ae3ff44ab9e35835b085239c258b6fac3074}"
