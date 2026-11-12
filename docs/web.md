# Penggunaan web lokal

**Status:** antarmuka web lokal telah diimplementasikan. Tes offline HTTP/worker mencakup gate,
keamanan, pemulihan, dan backup/restore; pengujian native Windows/macOS tetap diperlukan.
Spesifikasi CLI v0.2 tetap menjadi catatan
historis; perubahan batas produknya dicatat di [addendum web](superpowers/specs/2026-11-12-quality-platform-web-addendum.md).

## Persiapan dan menjalankan layanan

Web ditujukan untuk satu pemilik, satu mesin, SQLite lokal, dan satu worker pemilik hak tulis.
Jalankan `uv sync --dev`, lalu sesuaikan [template config.yaml](../config.yaml): ganti seluruh
placeholder `model_id` dan pilih provider yang memiliki credential Anda. Lihat juga
[panduan CLI](cli.md#projects-and-workflows).
Pilih provider/model secara eksplisit; tidak ada model default atau fallback otomatis. Gunakan
path absolut untuk konfigurasi dan direktori state agar semua proses membuka instalasi yang sama.

```bash
uv run qa-web init --config /absolute/config.yaml --state /absolute/web-state --owner qa
```

`init` meminta kata sandi dan konfirmasi tanpa menampilkannya. Jangan masukkan kata sandi lewat
argumen shell. State menyimpan path konfigurasi server, pengaturan non-rahasia, serta database
kontrol dengan hash kata sandi, bukan kata sandi plaintext. Lindungi direktori state dengan hak
akses pemilik; state dan workspace bukan bahan untuk commit atau dibagikan.

Kredensial `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, atau `GOOGLE_API_KEY` hanya disediakan lewat
environment proses server/worker yang memerlukannya. `.env` tidak otomatis dimuat. Tidak ada
formulir web untuk API key; jangan masukkan rahasia ke instruksi, dokumen sumber, YAML model,
kode, log, atau laporan. Browser tidak menerima kredensial provider.

Jalankan dua proses berikut di terminal terpisah dengan state yang sama:

```bash
uv run qa-web worker --state /absolute/web-state
uv run qa-web serve --state /absolute/web-state --port 8000
```

Server secara default hanya mendengarkan loopback. Buka alamat loopback pada mesin tempat
layanan berjalan dan masuk sebagai pemilik yang dibuat saat `init`. Worker memproses antrean
persisten; server menyajikan UI dan mencatat permintaan. Menjalankan `init`, worker, atau server
tidak boleh otomatis memicu panggilan model berbayar maupun deployment remote.

Dukungan Windows native dituju, tetapi **belum diuji dalam orb Linux ini**. Contoh `/absolute/...`
adalah path POSIX; di PowerShell gunakan path absolut Windows dengan tanda kutip bila perlu.
Jangan menyimpulkan kompatibilitas Windows dari hasil tes Linux saja.

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

Laporan benchmark/release di web bersifat **read-only**: melihat laporan tidak menjalankan
benchmark, menyetujui release, atau mempromosikan baseline. Benchmark berbayar tetap melalui
perintah eksplisit dan konfirmasi budget sesuai [panduan benchmarking](benchmarking.md).
Tes offline dengan fake model bukan bukti kualitas benchmark berbayar atau kesiapan rilis.

## Antrean, restart, dan akses CLI

Antrean disimpan di state kontrol, bukan hanya memori server. Mematikan server tidak berarti
worker berhenti; hentikan keduanya bila ingin menghentikan layanan sepenuhnya. Setelah restart,
periksa status antrean dan workflow sebelum melanjutkan. Job yang terinterupsi **tidak boleh
diulang otomatis**; resume harus dipilih secara eksplisit dengan identitas request/checkpoint
yang sama. Jangan membuat run baru sebagai pengganti resume tanpa meninjau pekerjaan sebelumnya.

Checkpoint dan idempotensi melindungi publikasi domain, tetapi bukan jaminan exactly-once untuk
provider. Jika respons provider sudah ditagihkan sebelum checkpoint tersimpan, resume dapat
mengulang panggilan dan menimbulkan **tagihan tambahan**. Tidak ada migrasi model/fallback saat resume.

Saat worker memegang lock penulis database, semua **mutasi CLI pada database yang sama dilarang**;
pembacaan CLI tetap diizinkan. Jangan membuka worker kedua atau mencoba melewati lock. Untuk
mutasi lewat CLI, hentikan worker terlebih dahulu dan pastikan hak tulis telah dilepas.
Pernyataan “tidak ada daemon/background worker” pada panduan CLI menjelaskan mode CLI mandiri,
bukan perluasan web ini.

## Akses remote: bukan default

Jangan mengekspos layanan loopback secara langsung. Akses remote memerlukan keputusan eksplisit,
HTTPS, public origin yang tepat, dan validasi allowed host. Contoh konfigurasi reverse proxy:

```bash
uv run qa-web serve --state /absolute/web-state --port 8000 --host 0.0.0.0 --public-origin https://qa.example.com
```

Ini bukan deployment otomatis; TLS harus diterminasi reverse proxy yang dikonfigurasi operator.
Origin publik harus cocok dengan host yang diizinkan; tanpa konfigurasi ini, binding non-loopback
harus ditolak. Jangan gunakan wildcard CORS, menyimpan kredensial dalam kode, atau mengandalkan
password saja tanpa HTTPS. Pastikan autentikasi, proteksi request mutasi, dan terminasi TLS diuji
sebelum membuka akses remote. Produk tetap single-owner, bukan layanan multi-tenant.

## Backup konsisten

1. Hentikan penerimaan pekerjaan baru, lalu hentikan **server, worker, dan semua penulis CLI**.
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

UI memakai `POST /commands` untuk form; klien JSON memakai `POST /api/commands` dengan field
`kind` (`create_project`, `create_workflow`, `run`, `resume`, `answer`, `scope`, `baseline`,
`submit`, `decide`, `revision`, `apply`, atau `scope_proposal`). Endpoint ini menggantikan
usulan route per aksi pada rancangan awal tanpa menggandakan implementasi domain. Upload tetap
melalui `POST /api/projects/{project}/workflows/{workflow}/sources`. Klien wajib memakai sesi
owner, CSRF token, dan `Idempotency-Key`; browser mendapat token dan command key melalui form.
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

Daftarkan ringkasan laporan benchmark yang telah ada secara eksplisit:

```bash
uv run qa-web register-report --state /absolute/web-state --project-id P1 --report /absolute/report.json
```

Halaman Reports tidak menerima path browser dan tidak menjalankan inference atau promotion.
