### Fixed

- **`repeat_of` rides every `GET /runs` row**, as the mentions contract says (a client folds a mention turn
  that repeats the person's own agent turn by it). Rows read from the run index carry no run vars, so the
  mention turn's row had no `repeat_of` on the default listing path. Every row now has the key: a mention
  turn names the turn it repeats, every other row says `null`. Only the mention door's turns can carry it
  (a client copy is dropped at `/runs/start`), so only those rows of the served page are read whole.
