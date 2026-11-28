# Vendored fonts

Geist (sans) and Geist Mono, from the npm package `geist@1.7.2` (Vercel), all six `.woff2` files
copied unmodified.

- Source: https://registry.npmjs.org/geist/-/geist-1.7.2.tgz
- npm integrity (sha512): `Gu5lDFa3pLRyoBlBPf0QIFHVdWAnpco7fS1bJm41jyLPFoguBgiubseUN2oLXMgqZ7uxAxDoXcHMhCY/fOTTgg==`
- Paths inside the package: `package/dist/fonts/geist-sans/Geist-<weight>.woff2`,
  `package/dist/fonts/geist-mono/GeistMono-<weight>.woff2`; weights Regular, Medium, SemiBold.
- License: SIL Open Font License 1.1 (`LICENSE-Geist.txt`, copied from `package/LICENSE.txt`).
- SHA256 of each file:
  - `Geist-Regular.woff2` (`package/dist/fonts/geist-sans/Geist-Regular.woff2`): `d8bce822db092746889bcf3f57350b41f53708b025458fe7af30729ec4ce0df2`
  - `Geist-Medium.woff2` (`package/dist/fonts/geist-sans/Geist-Medium.woff2`): `b0a0867cda44efef4529a4b13ce37fd9fd6e1597708615287542a51bc7452ab4`
  - `Geist-SemiBold.woff2` (`package/dist/fonts/geist-sans/Geist-SemiBold.woff2`): `b1e6a1dd2122485d0a1f3a8d30a45443aa9453224f83018bec35f8266bc77915`
  - `GeistMono-Regular.woff2` (`package/dist/fonts/geist-mono/GeistMono-Regular.woff2`): `e4507fb4fb5f832fbbb6c06aea4206274ba3083007f23fa8cbc0e87a10acf95b`
  - `GeistMono-Medium.woff2` (`package/dist/fonts/geist-mono/GeistMono-Medium.woff2`): `85b99e603f84a47dc8118b5af058ad8f387d7c507a00faeef1b5b60eb371e844`
  - `GeistMono-SemiBold.woff2` (`package/dist/fonts/geist-mono/GeistMono-SemiBold.woff2`): `8416445afd947018ffeb31844da808bd7f4356f5dc7d73a084659c32eae26548`

Rule: an update is a new vendoring with a new line here; the files are never edited by hand.
