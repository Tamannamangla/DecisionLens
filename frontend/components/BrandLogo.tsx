"use client";

import React from "react";
import Link from "next/link";
import { useTheme } from "@/context/ThemeContext";
import { DecisionLensIcon } from "@/components/DecisionLensLogo";

interface BrandLogoProps {
  className?: string;
  theme?: "light" | "dark";
  href?: string;
  showWordmark?: boolean;
  iconSizeClass?: string;
}

export default function BrandLogo({
  className = "",
  theme: propTheme,
  href = "/",
  showWordmark = true,
  iconSizeClass = "w-8 h-8",
}: BrandLogoProps) {
  const context = useTheme();
  const theme = propTheme || context.theme || "light";
  const isDark = theme === "dark";
  const inkPrimary = isDark ? "#F0EAE1" : "#26211b";

  return (
    <Link
      href={href}
      className={`group inline-flex items-center gap-2.5 transition-opacity hover:opacity-90 select-none ${className}`}
      aria-label="DecisionLens Home"
    >
      <div className={`${iconSizeClass} flex-shrink-0 flex items-center justify-center transition-transform duration-300 ease-out group-hover:scale-[1.05]`}>
        <DecisionLensIcon size={32} />
      </div>

      {showWordmark && (
        <div className="flex flex-col justify-center min-w-0">
          <span
            className="font-newsreader text-[18px] sm:text-[19px] font-semibold tracking-tight leading-snug whitespace-nowrap transition-colors"
            style={{ color: inkPrimary }}
          >
            DecisionLens<span className="text-[#b4874b] dark:text-[#FBBF24]">.</span>
          </span>
          <span className="text-[9px] font-mono font-bold uppercase tracking-wider text-[#9E7D47] dark:text-[#D4AA50] whitespace-nowrap leading-none mt-0.5">
            Executive Cognitive
          </span>
        </div>
      )}
    </Link>
  );
}
