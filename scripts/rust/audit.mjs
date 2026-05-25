#!/usr/bin/env zx
import 'zx/globals';

const advisories = [
  // ed25519-dalek: Double Public Key Signing Function Oracle Attack
  //
  // Remove once repo upgrades to ed25519-dalek v2
  'RUSTSEC-2022-0093',

  // curve25519-dalek
  //
  // Remove once repo upgrades to curve25519-dalek v4
  'RUSTSEC-2024-0344',

  // Crate:     tonic
  // Version:   0.9.2
  // Title:     Remotely exploitable Denial of Service in Tonic
  // Date:      2024-10-01
  // ID:        RUSTSEC-2024-0376
  // URL:       https://rustsec.org/advisories/RUSTSEC-2024-0376
  // Solution:  Upgrade to >=0.12.3
  'RUSTSEC-2024-0376',

  // Crate:     idna
  // Version:   0.1.5
  // Title:     `idna` accepts Punycode labels that do not produce any non-ASCII when decoded
  // Date:      2024-12-09
  // ID:        RUSTSEC-2024-0421
  // URL:       https://rustsec.org/advisories/RUSTSEC-2024-0421
  // Solution:  Upgrade to >=1.0.0
  // need to solve this dependency tree:
  // jsonrpc-core-client v18.0.0 -> jsonrpc-client-transports v18.0.0 -> url v1.7.2 -> idna v0.1.5
  'RUSTSEC-2024-0421',

  // The advisories below are all transitive dependencies pulled in via
  // solana-* / spl-stake-pool 2.3.x. Remove once we upgrade to a Solana
  // release that bumps these crates.

  // Crate:     bytes
  // Version:   1.10.0
  // Title:     Integer overflow in `BytesMut::reserve`
  // ID:        RUSTSEC-2026-0007
  // Solution:  Upgrade to >=1.11.1
  'RUSTSEC-2026-0007',

  // Crate:     time
  // Version:   0.3.37
  // Title:     Denial of Service via Stack Exhaustion
  // ID:        RUSTSEC-2026-0009
  // Solution:  Upgrade to >=0.3.47
  'RUSTSEC-2026-0009',

  // Crate:     quinn-proto
  // Version:   0.11.12
  // Title:     Denial of service in Quinn endpoints
  // ID:        RUSTSEC-2026-0037
  // Solution:  Upgrade to >=0.11.14
  'RUSTSEC-2026-0037',

  // Crate:     rustls-webpki
  // Version:   0.103.4
  // Title:     CRLs not considered authoritative by Distribution Point due to faulty matching logic
  // ID:        RUSTSEC-2026-0049
  // Solution:  Upgrade to >=0.103.10
  'RUSTSEC-2026-0049',

  // Crate:     tar
  // Version:   0.4.44
  // Title:     `unpack_in` can chmod arbitrary directories by following symlinks
  // ID:        RUSTSEC-2026-0067
  // Solution:  Upgrade to >=0.4.45
  'RUSTSEC-2026-0067',

  // Crate:     tar
  // Version:   0.4.44
  // Title:     tar-rs incorrectly ignores PAX size headers if header size is nonzero
  // ID:        RUSTSEC-2026-0068
  // Solution:  Upgrade to >=0.4.45
  'RUSTSEC-2026-0068',

  // Crate:     rustls-webpki
  // Version:   0.101.7, 0.103.4
  // Title:     Name constraints for URI names were incorrectly accepted
  // ID:        RUSTSEC-2026-0098
  // Solution:  Upgrade to >=0.103.12
  'RUSTSEC-2026-0098',

  // Crate:     rustls-webpki
  // Version:   0.101.7, 0.103.4
  // Title:     Name constraints were accepted for certificates asserting a wildcard name
  // ID:        RUSTSEC-2026-0099
  // Solution:  Upgrade to >=0.103.12
  'RUSTSEC-2026-0099',

  // Crate:     rustls-webpki
  // Version:   0.101.7, 0.103.4
  // Title:     Reachable panic in certificate revocation list parsing
  // ID:        RUSTSEC-2026-0104
  // Solution:  Upgrade to >=0.103.13
  'RUSTSEC-2026-0104',
];
const ignores = []
advisories.forEach(x => {
  ignores.push('--ignore');
  ignores.push(x);
});

// Check Solana version.
await $`cargo audit ${ignores}`;
