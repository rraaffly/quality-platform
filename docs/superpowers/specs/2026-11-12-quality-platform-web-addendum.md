# Quality Platform — Addendum Web untuk Spesifikasi v0.2

**Tanggal:** 2026-11-12

**Status saat ini:** arsip addendum transisi CLI/Web, bukan panduan operasional aktif. QA workflow CLI sudah dipensiunkan; batas laporan Web read-only telah digantikan benchmark, review, dan promosi melalui aksi Web eksplisit. Setup normal memakai `quality-platform setup`; `qa-web` tetap antarmuka operasional tingkat rendah. Ikuti [README](../../../README.md), [panduan Web](../../web.md), dan [Development Guidelines](../../../AGENTS.md). Isi berikut merekam keputusan dan batas verifikasi saat addendum disusun.

**Status pada saat penulisan:** perluasan web lokal diimplementasikan; verifikasi native lintas platform dan paid model belum dilakukan.

**Dasar:** [spesifikasi v0.2](2026-11-10-qa-orchestrator-mvp-python-v0.2-design.md).

**Panduan penggunaan:** [web lokal](../../web.md).

## 1. Hubungan dengan baseline historis

Spesifikasi v0.2 tidak ditulis ulang. Larangan web UI/always-on worker, pilihan CLI-only, serta
pernyataan bahwa proses CLI berhenti berarti eksekusi berhenti tetap mendeskripsikan baseline
historis CLI. Addendum ini memperluas batas tersebut untuk mode web: UI dan worker lokal yang
terpisah diizinkan. Menutup browser/server tidak otomatis menghentikan worker.

Aturan domain, provenance, isolasi proyek, immutable versioning, dependency closure, human gates,
static one-model-per-role, dan kebijakan evaluasi v0.2 tetap berlaku. Web bukan jalur pintas yang
boleh melewati validasi CLI/core. CLI mandiri tetap didukung; application/core bersama menjadi
batas antara adapter CLI/web dan domain/runtime.

## 2. Cakupan operasional

- Satu pemilik, mesin lokal, SQLite domain dan checkpoint lokal; bukan multi-tenant atau worker
  terdistribusi. Antrean job dan state kontrol persisten; worker memegang lock penulis database.
- Saat lock worker aktif, mutasi CLI terhadap database yang sama ditolak; pembacaan CLI tetap
  diizinkan. Web meneruskan mutasi melalui jalur worker yang sama, bukan penulis domain kedua.
- Entry point yang dituju:

  ```bash
  uv run qa-web init --config /absolute/config.yaml --state /absolute/web-state --owner qa
  uv run qa-web worker --state /absolute/web-state
  uv run qa-web serve --state /absolute/web-state --port 8000
  ```

- `init` meminta kata sandi beserta konfirmasi secara privat. State menyimpan path konfigurasi
  server, pengaturan non-rahasia, dan database kontrol dengan hash kata sandi; tidak menyimpan
  plaintext password atau kredensial provider. Kredensial hanya tersedia dalam environment
  server/worker yang membutuhkan, tidak di browser, form rahasia, kode, atau laporan.
- Native Windows merupakan target, bukan hasil verifikasi: orb saat ini Linux dan tidak dapat
  membuktikan dukungan Windows.

## 3. Kontrak UI dan approval

UI menyediakan alur proyek/workflow, impor sumber, instruksi dan status job, klarifikasi dengan
atribusi, review scope, baseline requirement, review artifact, revisi terarah, dan impact/staleness.
Snapshot konfigurasi workflow tetap immutable; perubahan model/config memerlukan workflow baru.
JSON test case tetap kanonis; detailed Markdown, Gherkin, atau keduanya adalah proyeksi deterministik.

Approval proposal scope, baseline requirement, test plan, dan test case adalah keputusan terpisah.
Approval mengikat versi/baseline/hash/checkpoint tepat; requirement baru dapat membuat approval
historis stale tanpa mengubah event lama. Apply targeted diff hanya menghasilkan draft baru dan
catatan persetujuan diff, **bukan approval final artifact**. ID dan membership requirement tidak
boleh berubah melalui revisi satu kasus. Impact detection tidak otomatis merevisi atau menerbitkan.

Laporan benchmark/release hanya ditampilkan read-only. Web tidak menjalankan benchmark berbayar,
memberikan release approval, atau mempromosikan baseline dari tindakan membaca laporan.

## 4. Eksekusi, pemulihan, dan biaya

Menyalakan layanan tidak memulai panggilan model atau deployment remote secara otomatis. Tindakan
live yang memanggil provider memerlukan konfirmasi eksplisit. Workflow **belum mempunyai monetary
cap**; batas concurrency bukan batas biaya dan budget benchmark tidak membatasi biaya workflow.

Antrean tetap ada setelah restart. Job terinterupsi harus ditandai untuk peninjauan dan **resume
eksplisit**, bukan diulang otomatis saat worker kembali. Pertahankan identitas request/checkpoint
dan immutable input/config. Idempotensi melindungi publikasi domain, bukan exactly-once billing:
panggilan yang telah ditagihkan tetapi belum tersimpan dapat terulang saat resume. Tidak ada
fallback provider atau migrasi model diam-diam.

## 5. Keamanan dan backup

Default serve adalah loopback. Remote serving hanya melalui konfigurasi eksplisit HTTPS public
origin dan allowed host. Flag yang tersedia adalah `--host 0.0.0.0` dan
`--public-origin https://host`; binding non-loopback tanpa origin/host valid harus ditolak.
Tidak ada wildcard CORS atau kredensial di kode. Autentikasi pemilik dan perlindungan request
mutasi harus tetap berlaku di jalur remote; publikasi remote bukan langkah otomatis instalasi.

Backup konsisten mensyaratkan server, worker, dan seluruh penulis CLI berhenti. Ambil satu set
database domain, database checkpoint, seluruh state kontrol, konfigurasi non-rahasia yang dirujuk,
dan workspace sumber/artifact. Jangan mengabaikan transaksi SQLite di WAL atau mencampur set
waktu berbeda. Lindungi hash akun dan data privat dalam backup. Kredensial environment dikelola
terpisah. Restore harus memulihkan keseluruhan set dan memeriksa path, izin, dan job terinterupsi
sebelum resume eksplisit.

## 6. Acceptance dan bukti pengujian

1. Init privat, autentikasi satu pemilik, penolakan request tanpa otorisasi, serta tidak ada rahasia
   provider di browser/log/laporan; default loopback dan penolakan host/origin remote tidak valid.
2. Core policy identik melalui CLI dan web: isolasi proyek, klarifikasi, scope/baseline/artifact
   gates, immutable config, detailed/Gherkin/JSON, targeted diff dan read-only reports.
3. Antrean bertahan setelah restart; job terinterupsi tidak otomatis diulang; resume eksplisit
   mempertahankan identitas dan tidak menggandakan publikasi domain.
4. Worker kedua/mutasi CLI ditolak ketika writer lock aktif; read CLI tetap berfungsi.
5. Backup/restore dengan layanan berhenti mempertahankan domain, checkpoint, kontrol dan workspace
   secara konsisten; pengujian Windows native dilakukan terpisah dari Linux.
6. Tes fake-model offline membuktikan perilaku aplikasi yang dicakup, **bukan** bukti benchmark
   model berbayar, kalibrasi evaluator, kualitas dunia nyata, atau kesiapan rilis.

Tes publik `tests/e2e/test_web_workflows.py` menguji gate, versi immutable, diff satu kasus,
resume tanpa pemanggilan ulang tugas selesai, import idempoten setelah crash, stale review,
isolasi download, hash/symlink, serta backup/restore pada path sama. Tes
`tests/integration/test_web_security.py` menguji autentikasi/CSRF/origin/host, expiry/rotasi sesi,
upload invalid/oversize, actor forgery, redaksi error provider, setting idempoten, serta laporan
read-only. `tests/integration/test_application_contract.py` menjaga kontrak CLI/application dan
writer ownership. Semua inference pengujian ini memakai fake offline, bukan provider berbayar.

Crash setelah keputusan domain berubah tetapi sebelum job finalization dapat menghasilkan
`BLOCKED` saat retry dengan reviewed-state hash lama. Ini sengaja fail-closed: pengguna meninjau
state terbaru, bukan mengulangi approval terhadap versi berbeda. Tidak ada transaksi atomik
lintas domain DB, checkpoint DB, dan control DB; tidak ada klaim exactly-once billing.

## 7. Referensi

- Baseline v0.2 merekam rancangan domain saat itu. Panduan QA CLI lama telah dipensiunkan; perilaku saat ini dirujuk melalui [panduan Web](../../web.md) dan kode/pengujian, bukan dengan menghidupkan kembali interface CLI lama.
- [Rancangan web yang diberikan](https://ampcode.com/artifacts/rancangan-web-quality-platform-608a8eaa-f2ec-4405-9d4b-82738a10d1be)
  menetapkan tahapan dan batas MVP. Implementasi memakai endpoint command bersama, bukan
  semua nama route per aksi yang masih berupa usulan pada rancangan.
