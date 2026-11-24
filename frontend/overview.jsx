import React, { useState } from "react";
import { createRoot } from "react-dom/client";

const currency = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const money = (cents) => currency.format(cents / 100);

function UnitCards({ units }) {
  return <div className="tw:my-5 tw:grid tw:grid-cols-2 tw:gap-3 tw:lg:grid-cols-4">
    {units.map(({ code, name, href, cents, status }) =>
      <a key={code} href={href}
         className="tw:block tw:min-w-0 tw:border tw:border-[var(--line)] tw:bg-[var(--card)] tw:px-4 tw:py-3 tw:hover:border-[var(--accent)]">
        <small>Apartamento {name}</small>
        <strong className="tw:block tw:text-lg tw:font-semibold tw:tracking-tight tw:tabular-nums">{money(cents)}</strong>
        {status && <small>{status}</small>}
      </a>
    )}
  </div>;
}

function pieSlices(categories) {
  const total = categories.reduce((sum, category) => sum + category.amount, 0);
  let angle = -Math.PI / 2;
  return categories.map((category) => {
    const sweep = category.amount / total * 2 * Math.PI;
    const end = angle + sweep;
    const middle = angle + sweep / 2;
    const point = (radians) => `${60 + 53 * Math.cos(radians)} ${60 + 53 * Math.sin(radians)}`;
    const path = sweep >= 2 * Math.PI
      ? "M 60 7 A 53 53 0 1 1 60 113 A 53 53 0 1 1 60 7 Z"
      : `M 60 60 L ${point(angle)} A 53 53 0 ${sweep > Math.PI ? 1 : 0} 1 ${point(end)} Z`;
    angle = end;
    return { ...category, path, x: 5 * Math.cos(middle), y: 5 * Math.sin(middle) };
  });
}

function ExpenseChart({ categories }) {
  const [hovered, setHovered] = useState(null);
  const [focused, setFocused] = useState(null);
  const active = hovered ?? focused;
  const slices = pieSlices(categories);
  return <>
    <h2>Despesas por categoria</h2>
    {slices.length ? <>
      <svg aria-hidden="true" className="expense-pie expense-pie-animated expense-pie-interactive" viewBox="0 0 120 120"
           onPointerLeave={() => setHovered(null)}>
        {slices.map(({ name, path, color, x, y }) =>
          <path key={name} d={path} fill={color} className="expense-slice"
                data-active={active === name}
                style={{ "--slice-x": `${x}px`, "--slice-y": `${y}px` }} />
        )}
        {slices.map(({ name, path, amount }) =>
          <path key={name} d={path} fill="transparent"
                onPointerEnter={(event) => { if (event.pointerType !== "touch") setHovered(name); }}>
            <title>{name}: {money(amount)}</title>
          </path>
        )}
      </svg>
      <ul className="tw:m-0 tw:list-none tw:p-0 tw:divide-y tw:divide-[var(--line)]">
        {categories.map(({ name, amount, color }) =>
          <li key={name} tabIndex={0} className="expense-legend-entry tw:grid tw:grid-cols-[10px_minmax(0,1fr)_auto] tw:items-center tw:gap-2 tw:py-2 tw:text-sm"
              data-active={active === name}
              onPointerEnter={(event) => { if (event.pointerType !== "touch") setHovered(name); }}
              onPointerLeave={() => setHovered(null)}
              onFocus={() => setFocused(name)} onBlur={() => setFocused(null)}>
            <span aria-hidden="true" className="tw:size-2.5 tw:rounded-full" style={{ backgroundColor: color }} />
            <span>{name}</span><strong>{money(amount)}</strong>
          </li>
        )}
      </ul>
    </> : <p className="muted">As despesas lançadas aparecem aqui.</p>}
  </>;
}

const data = document.getElementById("patio-overview-data");
if (data) {
  const { units, categories } = JSON.parse(data.textContent);
  createRoot(document.getElementById("patio-unit-cards")).render(<UnitCards units={units} />);
  createRoot(document.getElementById("patio-expense-chart")).render(<ExpenseChart categories={categories} />);
}
