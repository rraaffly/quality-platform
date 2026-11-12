document.addEventListener("htmx:responseError", (event) => {
  const notice = document.getElementById("notice");
  if (!notice) return;
  notice.textContent =
    event.detail.xhr.status === 409
      ? "Versi berubah. Muat ulang halaman dan tinjau kembali sebelum memutuskan."
      : "Permintaan tidak dapat diproses. Periksa isian, sesi login, dan prasyarat.";
  notice.classList.add("visible");
});
