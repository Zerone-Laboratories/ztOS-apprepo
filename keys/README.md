# The store key

`store.pub` goes here once it exists: the minisign public key every ztOS
phone trusts for the store (ztstore compiles it in, `zrn-exec` reads it from
`/usr/share/zrn/trusted-keys/store.pub`). The secret key never enters this
repository or any laptop that builds apps.

Making it (the owner, once, on a clean machine):

```
zrn keygen --output store.key --no-trust     # writes store.key (secret) and store.pub
```

- `store.pub`: commit it here as `keys/store.pub`.
- `store.key`: paste its contents into the GitHub environment secret
  `ZRN_STORE_KEY` of the `store-signing` environment, keep one offline copy
  (encrypted, e.g. on paper or a hardware token), then delete the file.

Rotation: make a new key, ship ztstore trusting both, re-sign the current
releases with the new key (re-run publish for them), then drop the old key
from clients. If the key leaks, revoke trust in the old key in the same
ztstore update and say so on Discord.
