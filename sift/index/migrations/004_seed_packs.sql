-- Track which seed packs have been loaded, for `sift seed status`.
CREATE TABLE seed_packs (
    name          text PRIMARY KEY,
    version       int NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    passage_count int NOT NULL
);
