//! Breach and audit escalation policy layered over [`BasisObserver`].

use crate::observer::{BasisObserver, SystemHealth, WindowStats};

pub const MAHALANOBIS_BREACH: f64 = 3.0;
pub const CRITICALITY_WINDOW: usize = 100;
pub const CRITICALITY_AUDIT_PCT: f64 = 15.0;
pub const AUDIT_COOLDOWN_TICKS: usize = 25;
pub const INNOVATION_STREAM_LEN: usize = 50;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventLevel {
    Monitoring,
    Breach,
    Audit,
}

impl EventLevel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Monitoring => "MONITORING",
            Self::Breach => "BREACH",
            Self::Audit => "AUDIT",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SentinelSnapshot {
    pub innovation_stream: Vec<f64>,
    pub window_metrics: WindowStats,
    pub filtered_basis: f64,
    pub velocity: f64,
    pub criticality_full_ring_pct: f64,
    pub criticality_recent_window_pct: f64,
    pub breach_health: Option<SystemHealth>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SentinelEvent {
    pub level: EventLevel,
    pub health: SystemHealth,
    pub criticality_recent_pct: f64,
    pub snapshot: Option<SentinelSnapshot>,
    pub breach: bool,
}

pub struct SentinelPolicy {
    observer: BasisObserver,
    criticality_window: usize,
    audit_criticality_pct: f64,
    audit_cooldown_ticks: usize,
    last_breach_health: Option<SystemHealth>,
    audit_active: bool,
    event_tick: usize,
    last_audit_snapshot_tick: Option<usize>,
}

impl SentinelPolicy {
    pub fn new(
        observer: BasisObserver,
        criticality_window: usize,
        audit_criticality_pct: f64,
        audit_cooldown_ticks: usize,
    ) -> Result<Self, String> {
        if !audit_criticality_pct.is_finite() {
            return Err("audit criticality percentage must be finite".into());
        }
        Ok(Self {
            observer,
            criticality_window,
            audit_criticality_pct,
            audit_cooldown_ticks,
            last_breach_health: None,
            audit_active: false,
            event_tick: 0,
            last_audit_snapshot_tick: None,
        })
    }

    pub fn process_event(
        &mut self,
        timestamp: f64,
        physical_price: f64,
        token_price: f64,
    ) -> SentinelEvent {
        let health = self.observer.update(timestamp, physical_price, token_price);
        let criticality_recent_pct = self.observer.criticality_recent(self.criticality_window);
        let event_tick = self.event_tick;
        self.event_tick += 1;
        let breach = health.measurement_valid && health.mahalanobis_distance > MAHALANOBIS_BREACH;

        if breach {
            self.last_breach_health = Some(health.clone());
        }

        let level = event_level(&health, criticality_recent_pct, self.audit_criticality_pct);
        let mut snapshot = None;
        if level == EventLevel::Audit {
            let should_snapshot = !self.audit_active
                || self.audit_cooldown_ticks == 0
                || self.last_audit_snapshot_tick.is_none()
                || event_tick.saturating_sub(self.last_audit_snapshot_tick.unwrap_or(event_tick))
                    >= self.audit_cooldown_ticks;
            self.audit_active = true;
            if should_snapshot {
                snapshot = Some(self.build_snapshot(
                    if breach {
                        Some(health.clone())
                    } else {
                        self.last_breach_health.clone()
                    },
                    criticality_recent_pct,
                ));
                self.last_audit_snapshot_tick = Some(event_tick);
            }
        } else {
            self.audit_active = false;
        }

        SentinelEvent {
            level,
            health,
            criticality_recent_pct,
            snapshot,
            breach,
        }
    }

    fn build_snapshot(
        &self,
        breach_health: Option<SystemHealth>,
        criticality_recent_window_pct: f64,
    ) -> SentinelSnapshot {
        let (filtered_basis, velocity) = self.observer.basis_velocity();
        SentinelSnapshot {
            innovation_stream: self.observer.last_innovations(INNOVATION_STREAM_LEN),
            window_metrics: self.observer.window_stats(),
            filtered_basis,
            velocity,
            criticality_full_ring_pct: self.observer.criticality_score(),
            criticality_recent_window_pct,
            breach_health,
        }
    }

    pub fn criticality_window(&self) -> usize {
        self.criticality_window
    }

    pub fn audit_criticality_pct(&self) -> f64 {
        self.audit_criticality_pct
    }

    pub fn audit_cooldown_ticks(&self) -> usize {
        self.audit_cooldown_ticks
    }

    pub fn window_stats(&self) -> WindowStats {
        self.observer.window_stats()
    }

    pub fn criticality_score(&self) -> f64 {
        self.observer.criticality_score()
    }

    pub fn criticality_recent(&self, window: usize) -> f64 {
        self.observer.criticality_recent(window)
    }

    pub fn last_innovations(&self, count: usize) -> Vec<f64> {
        self.observer.last_innovations(count)
    }

    pub fn basis_velocity(&self) -> (f64, f64) {
        self.observer.basis_velocity()
    }
}

fn event_level(
    health: &SystemHealth,
    criticality_recent_pct: f64,
    audit_criticality_pct: f64,
) -> EventLevel {
    if criticality_recent_pct > audit_criticality_pct {
        EventLevel::Audit
    } else if health.measurement_valid && health.mahalanobis_distance > MAHALANOBIS_BREACH {
        EventLevel::Breach
    } else {
        EventLevel::Monitoring
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::observer::ObserverInit;

    fn health(mahalanobis_distance: f64) -> SystemHealth {
        SystemHealth {
            filtered_basis: 0.0,
            innovation: 0.0,
            mahalanobis_distance,
            measurement_valid: true,
        }
    }

    #[test]
    fn breach_threshold_is_strictly_greater_than() {
        assert_eq!(
            event_level(
                &health(MAHALANOBIS_BREACH - 1e-12),
                0.0,
                CRITICALITY_AUDIT_PCT,
            ),
            EventLevel::Monitoring,
        );
        assert_eq!(
            event_level(
                &health(MAHALANOBIS_BREACH + 1e-12),
                0.0,
                CRITICALITY_AUDIT_PCT,
            ),
            EventLevel::Breach,
        );
    }

    #[test]
    fn audit_threshold_is_strictly_greater_than() {
        assert_eq!(
            event_level(
                &health(0.0),
                CRITICALITY_AUDIT_PCT - 1e-12,
                CRITICALITY_AUDIT_PCT,
            ),
            EventLevel::Monitoring,
        );
        assert_eq!(
            event_level(
                &health(0.0),
                CRITICALITY_AUDIT_PCT + 1e-12,
                CRITICALITY_AUDIT_PCT,
            ),
            EventLevel::Audit,
        );
    }

    #[test]
    fn sustained_audit_respects_snapshot_cooldown() {
        let observer = BasisObserver::new(ObserverInit {
            name: "sentinel-test".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: None,
            ring_buffer_capacity: Some(1000),
        })
        .unwrap();
        let mut policy = SentinelPolicy::new(observer, 100, -1.0, 3).unwrap();
        let snapshots: Vec<usize> = (0..7)
            .filter(|tick| {
                policy
                    .process_event(*tick as f64, 100.0, 100.0)
                    .snapshot
                    .is_some()
            })
            .collect();
        assert_eq!(snapshots, vec![0, 3, 6]);
    }

    #[test]
    fn sustained_audit_is_not_diluted_by_invalid_ticks() {
        let observer = BasisObserver::new(ObserverInit {
            name: "invalid-audit-test".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-8),
            ring_buffer_capacity: Some(1000),
        })
        .unwrap();
        let mut policy = SentinelPolicy::new(
            observer,
            CRITICALITY_WINDOW,
            CRITICALITY_AUDIT_PCT,
            AUDIT_COOLDOWN_TICKS,
        )
        .unwrap();
        for tick in 0..20 {
            policy.process_event(tick as f64, 100.0, 100.0);
        }
        let mut elevated = None;
        for tick in 20..40 {
            elevated = Some(policy.process_event(tick as f64, 130.0, 100.0));
        }
        assert_eq!(elevated.unwrap().level, EventLevel::Audit);

        for tick in 40..240 {
            let event = policy.process_event(tick as f64, 100.0, 0.0);
            assert!(!event.health.measurement_valid);
            assert_eq!(event.level, EventLevel::Audit);
        }
    }
}
