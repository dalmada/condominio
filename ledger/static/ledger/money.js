function formatReais(digits) {
  if (!digits) return "";
  const cents = digits.padStart(3, "0");
  const integer = cents.slice(0, -2).replace(/^0+(?=\d)/, "");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `R$ ${grouped},${cents.slice(-2)}`;
}

for (const field of document.querySelectorAll("[data-money]")) {
  field.addEventListener("input", () => {
    field.value = formatReais(field.value.replace(/\D/g, ""));
  });
  field.addEventListener("paste", (event) => {
    const text = event.clipboardData.getData("text").trim().replace(/^R\$\s*/, "");
    const parts = text.match(/^(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?$/);
    event.preventDefault();
    if (!parts) return;
    field.value = formatReais(parts[1].replace(/\./g, "") + (parts[2] || "").padEnd(2, "0"));
  });
}
