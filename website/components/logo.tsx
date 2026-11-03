import Link from "next/link";
export function Logo({ light = false }: { light?: boolean }) {
  return <Link href="/" className={`brand ${light ? "brand-light" : "brand-dark"}`} aria-label="Allen and Cooper — home">
    <span className="brand-symbol" aria-hidden="true">A<span className="brand-amp">&</span>C</span>
    <span className="brand-text"><strong>ALLEN <span>&</span> COOPER</strong><small>INVESTMENT RESEARCH</small></span>
  </Link>;
}
