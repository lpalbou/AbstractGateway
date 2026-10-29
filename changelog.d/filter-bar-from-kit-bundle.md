### Changed

- **The Models page's filter bar comes from the vendored kit only** (follow-up to the kit filter bar). The console
  vendored ui-kit 0.9.9, which carries the filter bar, so its interim copies are gone: the `af-filterbar` CSS block
  (`KIT_FILTER_BAR_CSS`) and the markup port (`afFilter*` in the catalog script). The page serves the kit's block from
  the vendored ISLANDS_CSS, and the chips and scope buttons are the bundle's `filterBarFacetsHtml` /
  `filterBarScopeButtonsHtml`. A bundle without them makes the Models page say so; it never draws a different bar.
