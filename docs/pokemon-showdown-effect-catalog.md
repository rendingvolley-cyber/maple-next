# Pokémon Showdown reference attribution

Maple Battle Record v5's offline effect catalog uses Pokémon Showdown's
machine-readable move, ability, and species data as a reference.

- Upstream: https://github.com/smogon/pokemon-showdown
- Pinned commit: `6a1836dd71c0718e923206f3d089e61074410868`
- Referenced paths: `data/moves.ts`, `data/abilities.ts`, `data/pokedex.ts`
- Upstream license: MIT

The Maple catalog is a small curated operator-input aid. It is bundled
locally, performs no runtime Showdown fetch, never estimates damage, and
never applies an effect without an explicit human Apply action.

