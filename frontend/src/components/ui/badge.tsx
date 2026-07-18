import { cva, type VariantProps } from "class-variance-authority"
import * as React from "react"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium font-mono-nums transition-colors",
  {
    variants: {
      variant: {
        default: "border-zinc-700 bg-zinc-900 text-zinc-200",
        success: "border-emerald-800/80 bg-emerald-950/50 text-emerald-300",
        warning: "border-amber-800/80 bg-amber-950/40 text-amber-200",
        destructive: "border-red-800/80 bg-red-950/40 text-red-300",
        outline: "border-zinc-600 text-zinc-300",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge }
