## Summary

<!-- What changed and why. One logical change per PR. -->

## Invariant affected

<!-- Which I1..I4 property, endpoint, tool, or UI surface does this touch?
     If none, say "docs only". -->

## Gates run

<!-- Paste real output, including any red readings you fixed. -->

```console
$ python3.12 -m unittest discover -s tests -t . -v
$ python3.12 -m compileall -q backend
$ python3.12 -m ruff check backend tests packaging
$ python3.12 packaging/verify_qp_plugin.py .
$ node --check ui/index.js
$ bash packaging/build_zip.sh
```

## Negative control

<!-- Remove the defence, show the gate going red, restore, show it green.
     A gate that cannot fail is not a gate. -->

## Docs

- [ ] Spec/plan updated if behaviour drifted
- [ ] README (both languages) updated if user-visible behaviour changed
- [ ] No real secrets in the diff (tests use DUMMY-SK-abcdef123456)
