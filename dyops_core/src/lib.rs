//! Dyops `dyops_core`: state-space observer for mean-reverting basis (OU) between physical and token prices.
//!
//! State `x = [basis, velocity, mean_level]`. The transition integrates mean-reversion speed `theta`
//! via an exact discrete-time map for the critically damped second-order OU structure.

mod observer;
mod sentinel;

pub use observer::{BasisObserver, ObserverInit, SystemHealth, WindowStats};
pub use sentinel::{
    EventLevel, SentinelEvent, SentinelPolicy, SentinelSnapshot, AUDIT_COOLDOWN_TICKS,
    CRITICALITY_AUDIT_PCT, CRITICALITY_WINDOW, INNOVATION_STREAM_LEN, MAHALANOBIS_BREACH,
};

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pymodule]
fn dyops_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyBasisObserver>()?;
    m.add_class::<PyDyopsSentinelCore>()?;
    m.add_class::<PySystemHealth>()?;
    m.add_class::<PyWindowStats>()?;
    m.add("MAHALANOBIS_BREACH", MAHALANOBIS_BREACH)?;
    m.add("CRITICALITY_WINDOW", CRITICALITY_WINDOW)?;
    m.add("CRITICALITY_AUDIT_PCT", CRITICALITY_AUDIT_PCT)?;
    m.add("AUDIT_COOLDOWN_TICKS", AUDIT_COOLDOWN_TICKS)?;
    Ok(())
}

/// Python-facing observer (wraps [`BasisObserver`]).
#[pyclass(name = "BasisObserver")]
struct PyBasisObserver {
    inner: BasisObserver,
}

#[pymethods]
impl PyBasisObserver {
    #[new]
    #[pyo3(signature = (name, theta, process_noise=None, measurement_noise=None, ring_buffer_capacity=None))]
    fn new(
        name: String,
        theta: f64,
        process_noise: Option<[f64; 9]>,
        measurement_noise: Option<f64>,
        ring_buffer_capacity: Option<usize>,
    ) -> PyResult<Self> {
        let init = ObserverInit {
            name,
            theta,
            q_process: process_noise,
            r_measurement: measurement_noise,
            ring_buffer_capacity,
        };
        let inner = BasisObserver::new(init).map_err(|e| PyErr::from(PyObserverError(e)))?;
        Ok(Self { inner })
    }

    fn update(&mut self, timestamp: f64, physical_price: f64, token_price: f64) -> PySystemHealth {
        let h = self.inner.update(timestamp, physical_price, token_price);
        PySystemHealth::from(h)
    }

    /// High-throughput batch path: one GIL hop and tight Rust loop over `float64` data.
    ///
    /// Arrays should be **one-dimensional**, **C-contiguous**, and **`float64`** (use
    /// `numpy.ascontiguousarray(..., dtype=numpy.float64)` when needed).
    #[pyo3(signature = (timestamps, physical, token))]
    fn update_batch<'py>(
        &mut self,
        py: Python<'py>,
        timestamps: PyReadonlyArray1<'py, f64>,
        physical: PyReadonlyArray1<'py, f64>,
        token: PyReadonlyArray1<'py, f64>,
    ) -> PyResult<Py<PyDict>> {
        let t = timestamps
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let p = physical
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let tok = token
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let n = t.len();
        if p.len() != n || tok.len() != n {
            return Err(PyValueError::new_err(
                "timestamps, physical, and token arrays must have the same length",
            ));
        }
        let (fb, inn, m) = self.inner.update_batch_slice(t, p, tok);

        let dict = PyDict::new(py);
        dict.set_item("filtered_basis", PyArray1::from_vec(py, fb))?;
        dict.set_item("innovation", PyArray1::from_vec(py, inn))?;
        dict.set_item("mahalanobis_distance", PyArray1::from_vec(py, m))?;
        Ok(dict.unbind())
    }

    /// Mean, variance, and Fisher excess kurtosis of innovations in the ring buffer.
    fn get_window_stats(&self) -> PyWindowStats {
        PyWindowStats::from(self.inner.window_stats())
    }

    /// Percentage of ring-buffer samples with Mahalanobis distance &gt; 3.0.
    fn get_criticality_score(&self) -> f64 {
        self.inner.criticality_score()
    }

    /// Criticality (`%` with Mahalanobis &gt; 3) over only the last `window` samples.
    fn get_criticality_recent(&self, window: usize) -> f64 {
        self.inner.criticality_recent(window)
    }

    /// Up to `n` most recent innovations from the ring (oldest → newest).
    fn get_last_innovations(&self, n: usize) -> Vec<f64> {
        self.inner.last_innovations(n)
    }

    /// `(filtered_basis, velocity)` from the internal state vector.
    fn get_basis_velocity(&self) -> (f64, f64) {
        self.inner.basis_velocity()
    }

    #[getter]
    fn name(&self) -> String {
        self.inner.name().to_string()
    }
}

/// Python-facing Rust sentinel policy owning an observer cloned from its current state.
#[pyclass(name = "DyopsSentinelCore")]
struct PyDyopsSentinelCore {
    inner: SentinelPolicy,
}

#[pymethods]
impl PyDyopsSentinelCore {
    #[new]
    #[pyo3(signature = (
        observer,
        criticality_window=CRITICALITY_WINDOW,
        audit_criticality_pct=CRITICALITY_AUDIT_PCT,
        audit_cooldown_ticks=AUDIT_COOLDOWN_TICKS as i64
    ))]
    fn new(
        observer: PyRef<'_, PyBasisObserver>,
        criticality_window: usize,
        audit_criticality_pct: f64,
        audit_cooldown_ticks: i64,
    ) -> PyResult<Self> {
        if audit_cooldown_ticks < 0 {
            return Err(PyValueError::new_err(
                "audit_cooldown_ticks must be non-negative",
            ));
        }
        let inner = SentinelPolicy::new(
            observer.inner.clone(),
            criticality_window,
            audit_criticality_pct,
            audit_cooldown_ticks as usize,
        )
        .map_err(PyValueError::new_err)?;
        Ok(Self { inner })
    }

    fn process_event<'py>(
        &mut self,
        py: Python<'py>,
        timestamp: f64,
        physical_price: f64,
        token_price: f64,
    ) -> PyResult<Py<PyDict>> {
        let event = self
            .inner
            .process_event(timestamp, physical_price, token_price);
        let result = PyDict::new(py);
        result.set_item("level", event.level.as_str())?;
        result.set_item("breach", event.breach)?;
        result.set_item("health", Py::new(py, PySystemHealth::from(event.health))?)?;
        result.set_item("criticality_recent_pct", event.criticality_recent_pct)?;
        match event.snapshot {
            Some(snapshot) => result.set_item("snapshot", snapshot_to_pydict(py, snapshot)?)?,
            None => result.set_item("snapshot", py.None())?,
        }
        Ok(result.unbind())
    }

    fn get_window_stats(&self) -> PyWindowStats {
        PyWindowStats::from(self.inner.window_stats())
    }

    fn get_criticality_score(&self) -> f64 {
        self.inner.criticality_score()
    }

    fn get_criticality_recent(&self, window: usize) -> f64 {
        self.inner.criticality_recent(window)
    }

    fn get_last_innovations(&self, count: usize) -> Vec<f64> {
        self.inner.last_innovations(count)
    }

    fn get_basis_velocity(&self) -> (f64, f64) {
        self.inner.basis_velocity()
    }

    #[getter]
    fn criticality_window(&self) -> usize {
        self.inner.criticality_window()
    }

    #[getter]
    fn audit_criticality_pct(&self) -> f64 {
        self.inner.audit_criticality_pct()
    }

    #[getter]
    fn audit_cooldown_ticks(&self) -> usize {
        self.inner.audit_cooldown_ticks()
    }
}

fn snapshot_to_pydict<'py>(
    py: Python<'py>,
    snapshot: SentinelSnapshot,
) -> PyResult<Bound<'py, PyDict>> {
    let result = PyDict::new(py);
    result.set_item("innovation_stream", snapshot.innovation_stream)?;

    let metrics = PyDict::new(py);
    set_finite_or_none(py, &metrics, "mean", snapshot.window_metrics.mean)?;
    set_finite_or_none(py, &metrics, "variance", snapshot.window_metrics.variance)?;
    set_finite_or_none(py, &metrics, "kurtosis", snapshot.window_metrics.kurtosis)?;
    result.set_item("window_metrics", metrics)?;

    let basis = PyDict::new(py);
    basis.set_item("filtered_basis", snapshot.filtered_basis)?;
    basis.set_item("velocity", snapshot.velocity)?;
    result.set_item("basis_state", basis)?;
    result.set_item(
        "criticality_full_ring_pct",
        snapshot.criticality_full_ring_pct,
    )?;
    result.set_item(
        "criticality_recent_window_pct",
        snapshot.criticality_recent_window_pct,
    )?;

    if let Some(health) = snapshot.breach_health {
        let breach = PyDict::new(py);
        breach.set_item("filtered_basis", health.filtered_basis)?;
        breach.set_item("innovation", health.innovation)?;
        breach.set_item("mahalanobis_distance", health.mahalanobis_distance)?;
        breach.set_item("measurement_valid", health.measurement_valid)?;
        result.set_item("breach_health", breach)?;
    }
    Ok(result)
}

fn set_finite_or_none(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    key: &str,
    value: f64,
) -> PyResult<()> {
    if value.is_finite() {
        dict.set_item(key, value)
    } else {
        dict.set_item(key, py.None())
    }
}

#[derive(Clone)]
#[pyclass(name = "SystemHealth")]
struct PySystemHealth {
    #[pyo3(get)]
    filtered_basis: f64,
    #[pyo3(get)]
    innovation: f64,
    #[pyo3(get)]
    mahalanobis_distance: f64,
    #[pyo3(get)]
    measurement_valid: bool,
}

impl From<SystemHealth> for PySystemHealth {
    fn from(h: SystemHealth) -> Self {
        Self {
            filtered_basis: h.filtered_basis,
            innovation: h.innovation,
            mahalanobis_distance: h.mahalanobis_distance,
            measurement_valid: h.measurement_valid,
        }
    }
}

#[derive(Clone)]
#[pyclass(name = "WindowStats")]
struct PyWindowStats {
    #[pyo3(get)]
    mean: f64,
    #[pyo3(get)]
    variance: f64,
    #[pyo3(get)]
    kurtosis: f64,
}

impl From<WindowStats> for PyWindowStats {
    fn from(s: WindowStats) -> Self {
        Self {
            mean: s.mean,
            variance: s.variance,
            kurtosis: s.kurtosis,
        }
    }
}

struct PyObserverError(String);

impl From<PyObserverError> for PyErr {
    fn from(e: PyObserverError) -> Self {
        PyValueError::new_err(e.0)
    }
}
