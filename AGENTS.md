# Local HD1910 deployment rules

- Upstream is `https://gitee.com/moheng_robot/microduck_rust.git`; keep `origin` unchanged.
- Preserve the 61D observation / 14D action policy contract. There are 15 servos; the mouth's
  bus ID is 34 (verified on hardware 2026-10-31), not 15, and it is never a policy action.
- Local scope is deliberately narrow: standalone voice + isolated beak, walking random voice, and
  read-only camera/detector diagnostics. Do not re-import the old repository's experimental motor,
  HOME, fall, policy-path, model, or safety edits.
- `--sound-only` must send no `robot.mouth` command at all. Mouth-enabled standalone voice may power
  only the beak while the body is limp.
- Camera detection tests must not connect to robotd or move servos.
- Run Python unit tests, Rust format/check/tests, and review `git diff` after every upstream rebase.
