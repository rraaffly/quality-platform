# Penggunaan web lokal

**Status:** aplikasi Web lokal dioperasikan melalui launcher `quality-platform` pada Windows, macOS, dan Linux. Spesifikasi CLI QA v0.2 adalah catatan historis; batas produk sekarang adalah Web.

## Persiapan dan menjalankan layanan

Web ditujukan untuk satu pemilik, satu mesin, SQLite lokal, dan satu worker pemilik hak tulis.
Distribusi aplikasi menggunakan wheel rilis melalui
`uv tool install --python 3.12 /path/to/quality_platform-0.1.0-py3-none-any.whl`, lalu
`uv tool update-shell` dan buka terminal baru. Wheel memuat backend, frontend hasil build,
dan resource benchmark sintetis publik; pengguna tidak memerlukan checkout, Bun, Node, atau Git.
uv mengelola environment Python terisolasi. Dependensi tetap perlu diunduh saat instalasi.

Installer source ZIP dan wrapper launcher PowerShell tidak disediakan. Untuk pengembangan
atau capture evidence, gunakan source checkout dengan `uv sync --locked --dev`, build frontend,
lalu jalankan CLI melalui `uv run quality-platform`. Bun hanya dibutuhkan untuk build frontend
dari source. Lihat [README](../README.md) untuk langkah build wheel dan pengaturan `PATH`.

Jalankan perintah berikut dari direktori mana pun:

```sh
quality-platform setup
quality-platform dashboard
quality-platform status
quality-platform stop
```

`setup` langsung menginisialisasi aplikasi tanpa pertanyaan model, nama, atau kata sandi.
Nama tampilan awal adalah `Owner`, dengan kata sandi awal `1234567890`. Login hanya meminta
kata sandi. Ubah nama tampilan dan kata sandi melalui **Settings → Account** dengan memasukkan
kata sandi saat ini. Kata sandi pengganti minimal 12 karakter; penggantian mencabut seluruh sesi
dan mewajibkan login ulang. Identitas audit tetap sama ketika nama tampilan diganti.
State hanya menyimpan hash kata sandi. Peringatan password awal tetap tampil sampai diganti,
dan mode remote tidak dapat dijalankan sebelum penggantian.

Data aplikasi selalu berada di home pengguna yang menjalankan CLI:

- Linux: `/home/<user>/.quality-platform`.
- macOS: `/Users/<user>/.quality-platform`.
- Windows: `%USERPROFILE%\.quality-platform`, biasanya `C:\Users\<user>\.quality-platform`.

Database domain ada di `domain.sqlite`, workspace default di `workspace/`, dan state kontrol,
antrean, serta log di `state/`. Tidak ada opsi `--data-dir`, `--skip-models`, `--config`,
`--owner`, atau `--password-stdin` pada setup. Tidak ada migrasi data lama.
`setup` berulang tidak mengubah akun, pengaturan aktif, atau data yang sudah ada.

Dashboard dan pembuatan proyek dapat digunakan tanpa model; pembuatan workflow dan benchmark
memerlukan konfigurasi seluruh agent terlebih dahulu. Pilih model melalui **Settings** atau:

```sh
quality-platform config model
quality-platform config workspace /absolute/path/to/workspace
```

`config model` menanyakan provider/model untuk empat agent tanpa memanggil provider.
Tidak ada model placeholder atau fallback otomatis. Workspace juga dapat diganti melalui
**Settings → Workspace location**; path Web harus absolut atau memakai `~/...`, sedangkan CLI
juga menerima path relatif terhadap direktori terminal. Direktori diperiksa agar bisa ditulis.
Perubahan workspace hanya berlaku untuk **proyek baru**. Proyek lama tetap memakai lokasi
semula, termasuk workflow baru dan impor dokumen di dalam proyek itu. Tidak ada pemindahan
file otomatis; database dan state aplikasi tetap berada di `.quality-platform`.
CLI dan Web memakai pengaturan berversi yang sama di database kontrol, tanpa perlu restart.
`config.yaml` adalah catatan bootstrap awal, bukan editor pengaturan aktif.

Kredensial `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, atau `GOOGLE_API_KEY` disediakan lewat environment
sebelum `dashboard` dinyalakan. `.env` tidak otomatis dimuat. Jangan masukkan rahasia ke YAML,
instruksi, dokumen, log, atau laporan. Browser tidak menerima kredensial provider. Jika mengubah
environment credential, jalankan `quality-platform stop` lalu `quality-platform dashboard` lagi.

`dashboard` memulai satu worker dan satu server loopback di background, menunggu health API, lalu
membuka browser. Ia tidak otomatis menjalankan model atau benchmark. `status` menunjukkan keadaan
layanan; `stop` menghentikan proses milik launcher. Log ada di `state/logs`, dan marker proses
menyimpan PID beserta waktu mulai untuk mencegah penghentian proses lain saat PID dipakai ulang.
Untuk port lain gunakan `quality-platform dashboard --port 8001`; untuk tidak membuka browser
gunakan `--no-open`. Hentikan layanan sebelum backup konsisten.

`qa-web serve` tetap tersedia sebagai perintah operasional tingkat rendah untuk konfigurasi server
lanjutan. Server dan UI berbagi origin agar cookie sesi HttpOnly dan proteksi CSRF bekerja.
Jika build frontend tidak ada pada source checkout, server mengembalikan HTTP 503 dengan petunjuk
build. Wheel rilis selalu mewajibkan build ini.

Sebelum upgrade wheel, hentikan layanan dan backup data, kemudian pasang wheel baru dengan
`uv tool install --force --python 3.12 /path/to/new-release.whl`. Direktori data tidak diganti.
Rollback aplikasi belum tentu kompatibel dengan schema database versi baru.

Capture workflow/policy evidence hanya tersedia pada source checkout dengan dependensi tes.
Jalankan benchmark dan capture bersama di checkout tersebut. Instalasi wheel menolak capture
karena tidak membawa test suite; laporan tidak otomatis menjadi bukti kelayakan rilis.
Fingerprint wheel mengikat kode/resource terpasang, bukan source tree. Commit Git dicatat
`unavailable` kecuali `QA_BENCHMARK_COMMIT_SHA` diberikan operator (bukan verifikasi Git).
Dataset real-world privat tetap dikelola melalui source checkout, bukan di dalam instalasi wheel.

## Alur kerja dan keputusan manusia

1. Buat/pilih proyek, mulai workflow, dan impor Markdown. Sumber disalin menjadi snapshot immutable;
   dokumen asli tidak diubah. Konfigurasi/model dipatok dalam snapshot workflow. Perubahan YAML
   tidak mengganti model workflow lama; perubahan konfigurasi memerlukan workflow baru.
2. Tinjau instruksi dan konfirmasikan **run live** sebelum memasukkan pekerjaan yang memanggil
   provider. **Tidak ada batas biaya moneter workflow.** `max_concurrency` hanya batas paralelisme,
   bukan batas token atau tagihan; gunakan pembatasan di provider bila tersedia.
3. Jawab klarifikasi dengan atribusi stakeholder dan sumber keputusan. Jawaban tidak otomatis
   menyelesaikan pertanyaan; analisis ulang menilai kecukupannya. Scope independen dapat lanjut
   sebagai draft, tetapi konflik/dependensi yang memblokir tidak boleh dilewati.
4. Lakukan review proposal scope, approval baseline requirement, dan approval artifact secara
   **terpisah**. Test plan dan test case juga memiliki gate masing-masing. Approval terikat pada
   versi, baseline, hash konten, dan checkpoint yang tepat; baseline stale menghalangi finalisasi.
5. Baca test case JSON kanonis dan proyeksi detailed Markdown/Gherkin (atau keduanya). Pilihan
   tampilan tidak mengganti model atau semantik test case.
6. Untuk revisi terarah, pilih kasus, tinjau diff, lalu terapkan diff yang tepat. Apply menghasilkan
   versi **draft**, bukan approval final. Versi lama tetap immutable; perubahan requirement
   menandai potensi stale, bukan menulis ulang artifact otomatis.

Membaca laporan tidak menjalankan benchmark atau mempromosikan baseline. Halaman Reports menyediakan job benchmark berbayar dengan konfirmasi biaya; halaman review menyediakan capture bukti, review blind, kalibrasi, keputusan release dan promosi eksplisit sesuai [panduan benchmarking](benchmarking.md).
Tes offline dengan fake model bukan bukti kualitas benchmark berbayar atau kesiapan rilis.

## Antrean dan restart

Antrean disimpan di state kontrol, bukan hanya memori server. Mematikan server tidak berarti
worker berhenti; hentikan keduanya bila ingin menghentikan layanan sepenuhnya. Setelah restart,
periksa status antrean dan workflow sebelum melanjutkan. Job yang terinterupsi **tidak boleh
diulang otomatis**; resume harus dipilih secara eksplisit dengan identitas request/checkpoint
yang sama. Jangan membuat run baru sebagai pengganti resume tanpa meninjau pekerjaan sebelumnya.

Checkpoint dan idempotensi melindungi publikasi domain, tetapi bukan jaminan exactly-once untuk
provider. Jika respons provider sudah ditagihkan sebelum checkpoint tersimpan, resume dapat
mengulang panggilan dan menimbulkan **tagihan tambahan**. Tidak ada migrasi model/fallback saat resume.

Jangan membuka worker kedua atau mencoba melewati lock penulis database.

## Akses remote: bukan default

Jangan mengekspos layanan loopback secara langsung. Akses remote memerlukan keputusan eksplisit,
penggantian password awal dari Web lokal, HTTPS, public origin yang tepat, dan validasi allowed
host. Server menolak public origin selama password awal masih digunakan; koneksi non-loopback
juga ditolak pada kondisi tersebut. Header forwarded tidak dipercaya untuk melewati pemeriksaan.
Contoh konfigurasi reverse proxy setelah password diganti:

```bash
uv run qa-web serve --state /absolute/web-state --port 8000 --host 0.0.0.0 --public-origin https://qa.example.com
```

Ini bukan deployment otomatis; TLS harus diterminasi reverse proxy yang dikonfigurasi operator.
Origin publik harus cocok dengan host yang diizinkan; tanpa konfigurasi ini, binding non-loopback
harus ditolak. Jangan gunakan wildcard CORS, menyimpan kredensial dalam kode, atau mengandalkan
password saja tanpa HTTPS. Pastikan autentikasi, proteksi request mutasi, dan terminasi TLS diuji
sebelum membuka akses remote. Produk tetap single-owner, bukan layanan multi-tenant.

## Backup konsisten

1. Hentikan penerimaan pekerjaan baru, lalu hentikan **server dan worker**.
   Catat job yang belum selesai; jangan menghapus state/checkpoint untuk memaksakan retry.
2. Salin sebagai satu set konsisten: database domain, database checkpoint, seluruh state kontrol
   (termasuk antrean, konfigurasi server dan hash akun), serta workspace sumber/artifact immutable.
   Sertakan konfigurasi YAML non-rahasia yang dirujuk. Gunakan backup SQLite yang benar; jangan
   menyalin hanya file `.sqlite` sambil mengabaikan file WAL yang masih memuat transaksi.
3. Simpan backup secara privat. Kredensial environment dikelola terpisah, bukan ditambahkan ke
   backup aplikasi. Saat restore, pulihkan seluruh set, periksa path absolut dan izin file,
   lalu periksa status job sebelum resume eksplisit dan menyalakan kembali layanan.

Jangan mencampur domain DB dari satu waktu dengan checkpoint/state/workspace dari waktu lain.
Tes offline memverifikasi salin/pulihkan instalasi berhenti pada path yang sama, termasuk antrean,
checkpoint, snapshot dan export artifact. Tetap uji prosedur backup sendiri sebelum dipakai pada data nyata.

## API perintah dan laporan

UI React memakai `POST /api/commands` dengan field
`kind` (`create_project`, `create_workflow`, `run`, `resume`, `answer`, `scope`, `baseline`,
`submit`, `decide`, `revision`, `apply`, atau `scope_proposal`). Endpoint ini menggantikan
usulan route per aksi pada rancangan awal tanpa menggandakan implementasi domain. Upload tetap
melalui `POST /api/projects/{project}/workflows/{workflow}/sources`. Klien wajib memakai sesi
owner, CSRF token, dan `Idempotency-Key`; browser memperoleh CSRF melalui `GET /api/session`
dan menyimpannya dalam memori, tidak membaca cookie. Login dan logout JSON memakai endpoint
`/login` dan `/logout`, dan sesi baru diputar setelah login. Pembacaan proyek, workflow, job,
source, artifact, settings dan report tersedia di `/api/*`; versi lama `/projects` dan `/jobs`
diarahkan ke route React. Upload multipart dengan `Accept: application/json` mengembalikan job
JSON, begitu juga retry JSON dengan konfirmasi biaya eksplisit.
Penyimpanan settings juga memakai key idempoten. Header/key sama dengan payload berbeda ditolak.

Command JSON diterima sebagai `202` beserta `job_id` dan `status_url`. Baca status di
`GET /api/jobs/{job_id}`; polling tidak mengulangi eksekusi. Status command `SUCCEEDED` bukan
approval artifact: hasil masih dapat berupa draft atau menunggu human review. Konflik yang
baru ditemukan worker berstatus `BLOCKED`, bukan perubahan terhadap HTTP response awal.

Jika crash terjadi setelah keputusan tersimpan tetapi sebelum job selesai, retry keputusan dengan
hash lama dapat ditolak sebagai `BLOCKED`. Tinjau artifact/baseline/history terkini; jangan
memaksakan keputusan pada versi baru. Run/import memakai identitas asli untuk melindungi
publikasi yang telah tersimpan. Review yang ditolak tetap ditolak dalam checkpoint; buat revisi
baru sebelum meminta approval berikutnya.

Halaman Reports dapat menerima unggahan laporan lama untuk inspeksi. Laporan impor tidak dapat menjadi bukti release; jalankan benchmark Web baru untuk jalur promosi. Browser hanya dapat mengubah path workspace melalui endpoint Settings khusus; path database, sumber, dan artifact tidak dapat ditentukan browser.
