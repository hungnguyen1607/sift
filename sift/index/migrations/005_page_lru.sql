-- LRU bookkeeping for bounded index growth: touch when a page is returned in
-- search results, evict least-recently-used pages beyond SIFT_MAX_PAGES.
ALTER TABLE pages ADD COLUMN last_used_at timestamptz NOT NULL DEFAULT now();
UPDATE pages SET last_used_at = fetched_at;
CREATE INDEX pages_last_used_idx ON pages (last_used_at);
