//
// This source file is part of the Nitro open source project.
//
// Copyright (c) 2026 Jaldis B.V.
//
// Licensed under the MIT OR Apache-2.0 license (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://opensource.org/licenses/MIT
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

//! The server object, its worker processes, and the parent that supervises them.
//!
//! Sockets are bound once, in the parent, before any worker exists. A port that
//! is already taken therefore fails at construction with one clear error rather
//! than once per worker after the process has appeared to start.
//!
//! Workers are forked. Each one is an ordinary process with its own runtime,
//! its own event loop and its own signal disposition, so nothing about shutting
//! one down has to be coordinated with its siblings. The fork happens before
//! any runtime is built, which is what keeps it safe: there are no other
//! threads in the process at that moment.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use nitro_core::config::ServerConfig;
use nitro_core::lifecycle::drain::{DrainCoordinator, DrainOutcome};
use nitro_core::lifecycle::init_tracing;
use nitro_core::lifecycle::signals::ShutdownController;
use nitro_core::router::{ParameterSpec, RouteDefinition, RouteTable};
use nitro_core::transport::accept::{self, BoundSockets};
use nitro_core::transport::tls::TlsMaterial;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;

use crate::config;
use crate::dispatch::PythonDispatch;
use crate::lifecycle;

/// How often the parent checks on its children.
const SUPERVISOR_POLL: Duration = Duration::from_millis(100);
/// How long the parent waits before replacing a worker that died, so a worker
/// that fails on startup cannot spin.
const RESPAWN_DELAY: Duration = Duration::from_millis(500);
/// Extra time beyond a worker's own drain budget before the parent stops
/// waiting and kills what is left.
const TERMINATION_GRACE: Duration = Duration::from_secs(5);

#[pyclass(name = "Server", module = "nitro._nitro")]
pub struct Server {
    application: Py<PyAny>,
    routes: Arc<RouteTable>,
    config: Arc<ServerConfig>,
    tls: Option<TlsMaterial>,
    /// Taken when serving starts, so a second call is refused rather than
    /// binding a second time.
    sockets: Mutex<Option<BoundSockets>>,
    addresses: Vec<(String, u16)>,
}

#[pymethods]
impl Server {
    /// Build a server for `application`, reading its settings from `settings`
    /// and binding every socket immediately.
    ///
    /// `routes` describes the route table: for each route its identifier, path,
    /// methods, and the parameters it captures as `(name, expression, greedy)`.
    #[new]
    #[pyo3(signature = (application, settings, routes=Vec::new()))]
    fn new(
        application: Py<PyAny>,
        settings: &Bound<'_, PyAny>,
        routes: Vec<RouteSpec>,
    ) -> PyResult<Self> {
        let config = config::server_config(settings)?;
        let routes = build_routes(routes)?;

        let tls = match &config.tls {
            Some(settings) => Some(
                TlsMaterial::load(settings)
                    .map_err(|error| PyValueError::new_err(error.to_string()))?,
            ),
            None => None,
        };

        // From here on the process is worth signalling, so make sure a signal
        // is recorded rather than fatal even before anything can act on it.
        process_signals::install().map_err(|error| PyRuntimeError::new_err(error.to_string()))?;

        let sockets =
            accept::bind(&config).map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        let addresses = sockets
            .local_addresses()
            .into_iter()
            .map(|address| (address.ip().to_string(), address.port()))
            .collect();

        Ok(Self {
            application,
            routes: Arc::new(routes),
            config: Arc::new(config),
            tls,
            sockets: Mutex::new(Some(sockets)),
            addresses,
        })
    }

    /// The addresses actually bound, useful when the port was left to the
    /// kernel to choose.
    #[getter]
    fn addresses(&self) -> Vec<(String, u16)> {
        self.addresses.clone()
    }

    #[getter]
    fn workers(&self) -> usize {
        self.config.workers
    }

    /// Serve until a termination signal arrives. Blocks the calling thread.
    fn serve(&self, python: Python<'_>) -> PyResult<()> {
        let sockets = match self.sockets.lock() {
            Ok(mut guard) => guard.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        let Some(sockets) = sockets else {
            return Err(PyRuntimeError::new_err(
                "this server has already been served",
            ));
        };

        if let Err(error) = init_tracing(&self.config.logging) {
            return Err(PyRuntimeError::new_err(error.to_string()));
        }

        if self.config.workers == 1 {
            run_worker(
                python,
                &self.application,
                Arc::clone(&self.routes),
                Arc::clone(&self.config),
                self.tls.clone(),
                sockets,
            )
            .map(|_| ())
        } else {
            supervise(
                python,
                &self.application,
                Arc::clone(&self.routes),
                Arc::clone(&self.config),
                self.tls.clone(),
                &sockets,
            )
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "Server(workers={}, addresses={:?})",
            self.config.workers, self.addresses
        )
    }
}

thread_local! {
    /// The thread state this runtime thread keeps for its lifetime, and the one
    /// it was running under before it gave the interpreter lock back.
    static ATTACHED: std::cell::Cell<Option<(pyo3::ffi::PyGILState_STATE, *mut pyo3::ffi::PyThreadState)>> =
        const { std::cell::Cell::new(None) };
}

/// Give every runtime thread a thread state that outlives the calls it makes.
///
/// Without this, a thread Python did not create pays for one on every call into
/// the interpreter: `Python::attach` ends in `PyGILState_Release`, which deletes
/// the state it had to create, and deleting it unmaps the data stack CPython
/// allocated for it. That is two system calls per request, and at the rates this
/// server is meant to serve they are a measurable fraction of what a request
/// costs.
///
/// Holding the state open costs nothing while the thread is idle: the lock is
/// handed straight back with `PyEval_SaveThread`, so what survives is the state,
/// not the lock. `PyGILState_Ensure` then finds it already there and returns
/// without allocating.
fn attach_runtime_threads(builder: &mut tokio::runtime::Builder) {
    builder
        .on_thread_start(|| {
            // SAFETY: the interpreter is initialised -- this runs on a thread
            // the runtime started from `run_worker`, which holds the lock.
            unsafe {
                let state = pyo3::ffi::PyGILState_Ensure();
                let saved = pyo3::ffi::PyEval_SaveThread();
                ATTACHED.set(Some((state, saved)));
            }
        })
        .on_thread_stop(|| {
            if let Some((state, saved)) = ATTACHED.take() {
                // SAFETY: paired with the `Ensure` above, on the same thread,
                // and nothing else has released this thread's state since.
                unsafe {
                    pyo3::ffi::PyEval_RestoreThread(saved);
                    pyo3::ffi::PyGILState_Release(state);
                }
            }
        });
}

/// Run the server in this process: build the runtime, construct the event loop,
/// run the startup hook, serve, then run the shutdown hook.
fn run_worker(
    python: Python<'_>,
    application: &Py<PyAny>,
    routes: Arc<RouteTable>,
    config: Arc<ServerConfig>,
    tls: Option<TlsMaterial>,
    sockets: BoundSockets,
) -> PyResult<DrainOutcome> {
    let mut builder = tokio::runtime::Builder::new_multi_thread();
    builder.worker_threads(config.runtime_threads).enable_all();
    attach_runtime_threads(&mut builder);
    pyo3_async_runtimes::tokio::init(builder);

    let asyncio = python.import("asyncio")?;
    let event_loop = asyncio.call_method0("new_event_loop")?;
    asyncio.call_method1("set_event_loop", (&event_loop,))?;

    let bound_application = application.bind(python);
    lifecycle::call_startup(bound_application, &event_loop)?;

    let locals = TaskLocals::new(event_loop.clone()).copy_context(python)?;
    let dispatch = PythonDispatch::new(
        python,
        application.clone_ref(python),
        routes,
        locals,
        config.stream_queue_capacity,
    )?;

    let controller = ShutdownController::new();
    let shutdown = controller.subscribe();
    let drain = DrainCoordinator::new(config.drain_timeout);

    // Installed before the loop starts rather than from inside it, so there is
    // no window in which a signal arriving right after startup falls through to
    // the default disposition and kills the process outright. Registration is
    // synchronous; the task that reacts to it runs once the loop is going.
    {
        let _runtime = pyo3_async_runtimes::tokio::get_runtime().enter();
        controller
            .watch_termination_signals()
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
    }
    // A signal delivered while the placeholder handler was in charge — between
    // binding and here — is recorded rather than acted on, so pick it up now.
    if process_signals::requested() {
        controller.trigger();
    }

    let outcome = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), async move {
        accept::serve(sockets, dispatch, config, tls, shutdown, drain)
            .await
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    });

    let shutdown_error = lifecycle::call_shutdown(bound_application, &event_loop);
    if let Err(error) = event_loop.call_method0("close") {
        tracing::warn!(error = %error.value(python), "closing the event loop failed");
    }

    match (outcome, shutdown_error) {
        (Err(error), _) => Err(error),
        (Ok(_), Some(error)) => Err(error),
        (Ok(outcome), None) => {
            if !outcome.is_complete() {
                tracing::warn!("the worker stopped before everything had drained");
            }
            Ok(outcome)
        }
    }
}

/// A route as the application describes it: identifier, path, methods, and each
/// captured parameter as `(name, expression, spans separators)`.
type RouteSpec = (u64, String, Vec<String>, Vec<(String, String, bool)>);

fn build_routes(specifications: Vec<RouteSpec>) -> PyResult<RouteTable> {
    let definitions = specifications
        .into_iter()
        .map(|(id, path, methods, parameters)| {
            RouteDefinition::new(id, path, methods).with_parameters(
                parameters
                    .into_iter()
                    .map(|(name, pattern, greedy)| {
                        let specification = ParameterSpec::new(name, pattern);
                        if greedy {
                            specification.greedy()
                        } else {
                            specification
                        }
                    })
                    .collect(),
            )
        });

    RouteTable::build(definitions).map_err(|error| PyValueError::new_err(error.to_string()))
}

// ── signals ──────────────────────────────────────────────────────────────────

/// A placeholder handler installed as soon as the sockets are bound.
///
/// It exists to cover the gap between a server being ready to talk about and
/// being ready to shut itself down: without it, a termination signal in that
/// window would take the default disposition and end the process abruptly.
/// Both the supervising parent and a worker consult the recorded flag once
/// they are able to act on it.
#[cfg(unix)]
mod process_signals {
    use std::sync::atomic::{AtomicBool, Ordering};

    static REQUESTED: AtomicBool = AtomicBool::new(false);

    /// Nothing may happen here beyond the store: this runs in signal context,
    /// where almost no library call is safe.
    extern "C" fn record(_signal: libc::c_int) {
        REQUESTED.store(true, Ordering::Release);
    }

    pub fn install() -> std::io::Result<()> {
        for signal in [libc::SIGINT, libc::SIGTERM] {
            // SAFETY: the handler only stores to an atomic, which is permitted
            // in signal context.
            let previous =
                unsafe { libc::signal(signal, record as *const () as libc::sighandler_t) };
            if previous == libc::SIG_ERR {
                return Err(std::io::Error::last_os_error());
            }
        }
        Ok(())
    }

    pub fn requested() -> bool {
        REQUESTED.load(Ordering::Acquire)
    }

    pub fn reset() {
        REQUESTED.store(false, Ordering::Release);
    }
}

#[cfg(not(unix))]
mod process_signals {
    pub fn install() -> std::io::Result<()> {
        Ok(())
    }

    pub fn requested() -> bool {
        false
    }

    pub fn reset() {}
}

/// Fork `workers` children, replace any that dies unexpectedly, and shut them
/// all down together when a signal arrives.
#[cfg(unix)]
fn supervise(
    python: Python<'_>,
    application: &Py<PyAny>,
    routes: Arc<RouteTable>,
    config: Arc<ServerConfig>,
    tls: Option<TlsMaterial>,
    sockets: &BoundSockets,
) -> PyResult<()> {
    let mut children: Vec<libc::pid_t> = Vec::with_capacity(config.workers);
    for index in 0..config.workers {
        children.push(fork_worker(
            python,
            application,
            &routes,
            &config,
            &tls,
            sockets,
            index,
        )?);
    }
    tracing::info!(workers = children.len(), "workers started");

    while !process_signals::requested() {
        match reap_one() {
            Some((pid, status)) => {
                let Some(slot) = children.iter().position(|child| *child == pid) else {
                    continue;
                };
                if process_signals::requested() {
                    break;
                }
                tracing::error!(pid, status, "worker exited unexpectedly; replacing it");
                std::thread::sleep(RESPAWN_DELAY);
                children[slot] =
                    fork_worker(python, application, &routes, &config, &tls, sockets, slot)?;
            }
            None => {
                // Releasing the interpreter while idling lets a signal handler
                // registered on the Python side run, and lets any other thread
                // make progress.
                python.detach(|| std::thread::sleep(SUPERVISOR_POLL));
            }
        }
    }

    tracing::info!("shutting workers down");
    stop_children(&children, config.drain_timeout + TERMINATION_GRACE);
    Ok(())
}

#[cfg(not(unix))]
fn supervise(
    python: Python<'_>,
    application: &Py<PyAny>,
    routes: Arc<RouteTable>,
    config: Arc<ServerConfig>,
    tls: Option<TlsMaterial>,
    sockets: &BoundSockets,
) -> PyResult<()> {
    Err(PyRuntimeError::new_err(
        "multiple workers require a platform that can fork; set workers to 1",
    ))
}

/// Fork one worker.
///
/// `os.fork` is used rather than the system call directly so the interpreter
/// runs its own after-fork bookkeeping in the child.
#[cfg(unix)]
#[allow(clippy::too_many_arguments)]
fn fork_worker(
    python: Python<'_>,
    application: &Py<PyAny>,
    routes: &Arc<RouteTable>,
    config: &Arc<ServerConfig>,
    tls: &Option<TlsMaterial>,
    sockets: &BoundSockets,
    index: usize,
) -> PyResult<libc::pid_t> {
    // Duplicated before the fork so the child owns descriptors of its own
    // rather than sharing the parent's, which it must not close.
    let inherited = duplicate(sockets, index)?;

    let pid: i32 = python
        .import("os")?
        .call_method0("fork")?
        .extract()
        .map_err(|error| PyRuntimeError::new_err(format!("fork failed: {error}")))?;

    if pid != 0 {
        tracing::debug!(pid, worker = index, "worker forked");
        return Ok(pid as libc::pid_t);
    }

    // The flag is inherited from the parent's memory. A worker that has only
    // just come into existence has not been signalled, so start it clear.
    process_signals::reset();

    let code = match run_worker(
        python,
        application,
        Arc::clone(routes),
        Arc::clone(config),
        tls.clone(),
        inherited,
    ) {
        Ok(_) => 0,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyKeyboardInterrupt>(python) => 0,
        Err(error) => {
            error.print(python);
            1
        }
    };

    // Leaving through `_exit` skips destructors and interpreter teardown, both
    // of which belong to the parent's copy of this state, not the child's.
    // SAFETY: no cleanup is owed in a forked child that is about to disappear.
    unsafe { libc::_exit(code) }
}

fn duplicate(sockets: &BoundSockets, worker: usize) -> PyResult<BoundSockets> {
    let describe =
        |error: std::io::Error| PyRuntimeError::new_err(format!("duplicating a socket: {error}"));

    Ok(BoundSockets {
        tcp: sockets
            .tcp
            .iter()
            .map(|listener| listener.try_clone().map_err(describe))
            .collect::<PyResult<Vec<_>>>()?,
        #[cfg(unix)]
        unix: match &sockets.unix {
            Some(listener) => Some(listener.try_clone().map_err(describe)?),
            None => None,
        },
        quic: sockets
            .quic
            .iter()
            .map(|socket| socket.try_clone().map_err(describe))
            .collect::<PyResult<Vec<_>>>()?,
        // A worker serves the endpoint bound for its own index, so scrapes of
        // one worker are not answered by another.
        metrics: match sockets.metrics.get(worker) {
            Some(endpoint) => vec![endpoint.duplicate().map_err(describe)?],
            None => Vec::new(),
        },
    })
}

/// Collect one exited child, if any is waiting. Returns `None` when every child
/// is still running or none is left.
#[cfg(unix)]
fn reap_one() -> Option<(libc::pid_t, i32)> {
    let mut status: libc::c_int = 0;
    // SAFETY: `waitpid` writes only through the pointer given, which is a live
    // local, and `WNOHANG` means it does not block.
    let pid = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
    if pid > 0 { Some((pid, status)) } else { None }
}

/// Ask every child to stop, wait for them, and kill whatever is still running
/// once the deadline passes.
#[cfg(unix)]
fn stop_children(children: &[libc::pid_t], timeout: Duration) {
    for &pid in children {
        // SAFETY: sending a signal to a known child process id.
        unsafe { libc::kill(pid, libc::SIGTERM) };
    }

    let deadline = Instant::now() + timeout;
    let mut outstanding: Vec<libc::pid_t> = children.to_vec();

    while !outstanding.is_empty() && Instant::now() < deadline {
        match reap_one() {
            Some((pid, _status)) => outstanding.retain(|child| *child != pid),
            None => std::thread::sleep(SUPERVISOR_POLL),
        }
    }

    for &pid in &outstanding {
        tracing::warn!(pid, "worker did not stop in time; killing it");
        // SAFETY: sending a signal to a known child process id.
        unsafe { libc::kill(pid, libc::SIGKILL) };
        let mut status: libc::c_int = 0;
        // SAFETY: reaping a child that has just been killed.
        unsafe { libc::waitpid(pid, &mut status, 0) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn the_parent_signal_flag_starts_clear_and_records_a_signal() {
        process_signals::reset();
        assert!(!process_signals::requested());

        process_signals::install().expect("installing handlers must work");
        // SAFETY: the handler installed above replaces the default disposition,
        // so this does not end the test process.
        unsafe { libc::raise(libc::SIGTERM) };

        assert!(process_signals::requested());
        process_signals::reset();
    }

    #[cfg(unix)]
    #[test]
    fn reaping_reports_nothing_when_there_are_no_children() {
        assert!(reap_one().is_none());
    }
}
