# content_key v1

`content_key` is the cross-source product identity key stored on
`catalog_products.content_key`.

## Inputs

- `brand`: required string after normalization.
- `title`: required string after normalization.
- `gtin`: optional string.

If normalized `brand` or normalized `title` is empty, the algorithm returns
`null`.

## Normalization

### Brand

1. Reject non-strings and empty strings as empty.
2. Trim and lowercase.
3. Remove registered/trademark marks: `®`, `™`, `(r)`, `(tm)`.
4. Split on whitespace.
5. Strip trailing corporate suffix tokens: `inc`, `llc`, `ltd`, `corp`,
   `co`, `company`, with optional trailing period/comma.
6. Collapse internal whitespace and trim.

### Title

1. Reject non-strings and empty strings as empty.
2. Unicode NFKD-normalize.
3. Drop combining marks.
4. Lowercase.
5. Replace all characters except word characters, whitespace, and hyphen with
   spaces. Hyphens are retained because they can carry product identity.
6. Collapse internal whitespace and trim.

### GTIN

1. Reject non-strings and empty strings as empty.
2. Strip all non-digits.
3. If the remaining digit string is 1-14 digits, left-pad to 14 digits.
4. If the remaining digit string is 15+ digits, leave it unchanged rather than
   truncating malformed data.

## Hash Construction

The canonical raw string is:

```text
<normalized_brand>::<normalized_title>::<normalized_gtin>
```

The key is:

```text
ck_ + sha256(raw_utf8).hexdigest()[0:32]
```

`services.catalog_identity.make_content_key` is the canonical Python
implementation. Other runtimes must match this contract bug-for-bug until a
new versioned contract is introduced.
