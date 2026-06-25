# Customer Call Log Profile — Standalone Tool

Phone number dile customer-er shob call history, status breakdown, order codes,
address — shob ekta page-e dekhabe.

## Setup (Windows, office/home PC)

1. Folder ta jekhane khushi rakho (e.g. `C:\Tools\customer-profile-tool`).
2. `.env.example` copy kore `.env` banao, tar bhitore `<host>` er jaygay actual
   DB host/IP din. Connection string-er user/password already bhora ache:

   ```
   DATABASE_URL=postgresql://dev_user:HEZZHhjSzu0J2nSNfDuYOWSj@<host>:5432/postgres
   ```

   Note: jodi 161.248.247.47 server-e direct Postgres port (5432) open thake,
   ota host hisebe dite parba. Na thakle DB-team/server admin-ke host confirm
   korte bolo.

3. `run.bat` e double-click koro. Eta automatically:
   - virtual environment banabe
   - dependencies install korbe
   - server start korbe `http://127.0.0.1:8010` e

4. Browser-e `http://127.0.0.1:8010` open koro, phone number diye search koro.

## Column auto-detect note

`customer_calls` table-er exact column names ami 100% sure chilam na, tai
`app.py` startup-e `information_schema` query kore actual columns dekhe
best-guess mapping banay (phone/name/status/date/note/order_code/address).

Jodi kichu field result-e missing/wrong dekho, ei URL-ta browser-e khulo
detected mapping dekhar jonno:

```
http://127.0.0.1:8010/api/columns
```

Eta dile bolo kon field-ta thik na — column map-ta manually fix kore dibo.

## Security note

`.env` file-ta `.gitignore`-e add kora ache, tai GitHub-e push korle credential
jabe na. Tobu, ei DB password ekhon ei conversation-e plaintext-e ache —
kaj shesh hole `dev_user`-er password rotate kore newa nirapod.
