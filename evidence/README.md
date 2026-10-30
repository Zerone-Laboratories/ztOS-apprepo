# Evidence

- `local-run/`: `run-local.sh` run on the laptop (30 October 2026): the sample
  app built twice at its pinned commit (same sha256), signed with a
  throwaway test key (`test-store-key.pub`; its secret was deleted), indexed,
  and checked as a client would; a client with another key refused it.
  The package is x86_64 because the laptop has no aarch64 compiler; CI builds
  aarch64 on arm64 runners (not run: nothing was pushed).
- `renders/`: the sample app (`hello-c.png`, and `hello-c-metro.png` in
  Metro) and the SDK's settings example in zrnCoolStyle dark, its Wi-Fi page,
  light, and the same two pages in Metro (`compare.png`). Drawn offscreen by
  libzrnui's GLES2 path (`zrn render`).
