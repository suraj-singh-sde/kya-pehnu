import { useRef, useState } from "react";
import { useFrame } from "@/lib/frame";

/** The photo frame on the wall. Decoration only: the picture is changed in Account. */
export function PhotoFrame() {
  const src = useFrame();
  return (
    <div className="frame" aria-hidden="true">
      <span className="fr">
        <span className="mat">
          <img src={src} alt="" />
        </span>
      </span>
    </div>
  );
}

const reduce = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/** A floor lamp with a pull string. Decoration only: drag or click the string to switch it on. */
export function Lamp() {
  const [on, setOn] = useState(false);
  const [drag, setDrag] = useState(false);
  const [dy, setDy] = useState(0);
  const y0 = useRef<number | null>(null);
  const dyRef = useRef(0);
  const toggle = () => {
    setOn((v) => !v);
  };
  const tug = () => {
    setDy(26);
    setTimeout(() => {
      setDy(0);
      toggle();
    }, reduce() ? 0 : 150);
  };
  const release = (e: React.PointerEvent) => {
    if (y0.current === null) return;
    y0.current = null;
    setDrag(false);
    setDy(0);
    if (e.type === "pointercancel") return;
    if (dyRef.current >= 14) toggle();
    else if (dyRef.current < 4) tug();
  };
  return (
    <div className={`lamp${on ? " on" : ""}${drag ? " drag" : ""}`} style={{ "--dy": dy } as React.CSSProperties}>
      <span className="glow" />
      <span className="cone" />
      <span className="pole" />
      <span className="foot" />
      <span className="bulb" />
      <span className="shade" />
      <button
        className="pull"
        aria-pressed={on}
        aria-label={`Lamp string. Pull to switch the light ${on ? "off" : "on"}`}
        title={`Pull to switch the light ${on ? "off" : "on"}`}
        onPointerDown={(e) => {
          y0.current = e.clientY;
          dyRef.current = 0;
          e.currentTarget.setPointerCapture(e.pointerId);
          setDrag(true);
        }}
        onPointerMove={(e) => {
          if (y0.current === null) return;
          dyRef.current = Math.max(0, Math.min(70, e.clientY - y0.current));
          setDy(dyRef.current);
        }}
        onPointerUp={release}
        onPointerCancel={release}
        onClick={(e) => e.detail === 0 && tug()}
      >
        <span className="cord" />
        <span className="bead" />
      </button>
    </div>
  );
}
