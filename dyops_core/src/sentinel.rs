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

    pub fn as_u8(self) -> u8 {
        match self {
            Self::Monitoring => 0,
            Self::Breach => 1,
            Self::Audit => 2,
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

pub struct SentinelBatch {
    pub filtered_basis: Vec<f64>,
    pub innovation: Vec<f64>,
    pub mahalanobis_distance: Vec<f64>,
    pub measurement_valid: Vec<bool>,
    pub level: Vec<u8>,
    pub criticality_recent_pct: Vec<f64>,
    pub breach: Vec<bool>,
    pub snapshot_emitted: Vec<bool>,
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

    pub fn process_batch_slice(
        &mut self,
        timestamps: &[f64],
        physical: &[f64],
        token: &[f64],
    ) -> SentinelBatch {
        let count = timestamps.len();
        debug_assert_eq!(count, physical.len());
        debug_assert_eq!(count, token.len());
        let mut batch = SentinelBatch {
            filtered_basis: Vec::with_capacity(count),
            innovation: Vec::with_capacity(count),
            mahalanobis_distance: Vec::with_capacity(count),
            measurement_valid: Vec::with_capacity(count),
            level: Vec::with_capacity(count),
            criticality_recent_pct: Vec::with_capacity(count),
            breach: Vec::with_capacity(count),
            snapshot_emitted: Vec::with_capacity(count),
        };
        for index in 0..count {
            let event = self.process_event(timestamps[index], physical[index], token[index]);
            batch.filtered_basis.push(event.health.filtered_basis);
            batch.innovation.push(event.health.innovation);
            batch
                .mahalanobis_distance
                .push(event.health.mahalanobis_distance);
            batch.measurement_valid.push(event.health.measurement_valid);
            batch.level.push(event.level.as_u8());
            batch
                .criticality_recent_pct
                .push(event.criticality_recent_pct);
            batch.breach.push(event.breach);
            batch.snapshot_emitted.push(event.snapshot.is_some());
        }
        batch
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

    #[test]
    fn policy_batch_matches_serial_events() {
        let init = ObserverInit {
            name: "policy-batch".into(),
            theta: 1.0,
            q_process: None,
            r_measurement: Some(1e-6),
            ring_buffer_capacity: Some(1000),
        };
        let mut serial = SentinelPolicy::new(
            BasisObserver::new(init.clone()).unwrap(),
            CRITICALITY_WINDOW,
            CRITICALITY_AUDIT_PCT,
            AUDIT_COOLDOWN_TICKS,
        )
        .unwrap();
        let mut batch = SentinelPolicy::new(
            BasisObserver::new(init).unwrap(),
            CRITICALITY_WINDOW,
            CRITICALITY_AUDIT_PCT,
            AUDIT_COOLDOWN_TICKS,
        )
        .unwrap();
        let timestamps: Vec<f64> = (0..240).map(|tick| tick as f64).collect();
        let physical = vec![100.0; 240];
        let token: Vec<f64> = (0..240)
            .map(|tick| {
                if (120..160).contains(&tick) {
                    90.0
                } else {
                    100.0
                }
            })
            .collect();
        let expected: Vec<SentinelEvent> = (0..timestamps.len())
            .map(|index| serial.process_event(timestamps[index], physical[index], token[index]))
            .collect();
        let actual = batch.process_batch_slice(&timestamps, &physical, &token);
        for (index, event) in expected.iter().enumerate() {
            assert_eq!(actual.filtered_basis[index], event.health.filtered_basis);
            assert_eq!(actual.innovation[index], event.health.innovation);
            assert_eq!(
                actual.mahalanobis_distance[index],
                event.health.mahalanobis_distance
            );
            assert_eq!(
                actual.measurement_valid[index],
                event.health.measurement_valid
            );
            assert_eq!(actual.level[index], event.level.as_u8());
            assert_eq!(
                actual.criticality_recent_pct[index],
                event.criticality_recent_pct
            );
            assert_eq!(actual.breach[index], event.breach);
            assert_eq!(actual.snapshot_emitted[index], event.snapshot.is_some());
        }
    }
}
