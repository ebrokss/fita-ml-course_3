# MySQL servera datu strukturas konteksts: `direct_payments`

Sis konteksts apraksta tikai datubazes strukturu, nevis tabulu datus.

## Kopsavilkums

- Datubaze: `direct_payments`
- Tabulu skaits: 3

## Tabulas un kolonnas

### `mandates`

- Tips: BASE TABLE

| kolonna | datu_tips | ierobezojumi | komentars |
| --- | --- | --- | --- |
| id | varchar(50) | - | - |
| created_at | datetime | - | - |
| scheme | varchar(50) | - | - |
| organisation_id | varchar(50) | - | - |

### `organisations`

- Tips: BASE TABLE

| kolonna | datu_tips | ierobezojumi | komentars |
| --- | --- | --- | --- |
| id | varchar(50) | - | - |
| created_at | datetime | - | - |
| parent_vertical | varchar(50) | - | - |

### `payments`

- Tips: BASE TABLE

| kolonna | datu_tips | ierobezojumi | komentars |
| --- | --- | --- | --- |
| id | varchar(50) | - | - |
| amount | double | - | - |
| currency | varchar(50) | - | - |
| created_at | datetime | - | - |
| source | varchar(50) | - | - |
| charge_date | datetime | - | - |
| mandate_id | varchar(50) | - | - |
