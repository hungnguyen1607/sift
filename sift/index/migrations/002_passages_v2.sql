-- Look pages up by normalized URL so http/https/www/utm variants share a row.
ALTER TABLE pages ADD COLUMN url_norm text;
UPDATE pages SET url_norm = url;
ALTER TABLE pages ALTER COLUMN url_norm SET NOT NULL;
ALTER TABLE pages DROP CONSTRAINT pages_url_key;
CREATE UNIQUE INDEX pages_url_norm_idx ON pages (url_norm);
-- Hash of the clean text: re-indexing an unchanged page skips re-embedding.
ALTER TABLE pages ADD COLUMN content_hash text;
ALTER TABLE passages ADD COLUMN heading text;
