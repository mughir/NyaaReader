"""
Real-Time Translation Progress Dashboard for NyaaReader

Provides live monitoring and visualization of translation jobs,
performance metrics, and queue status in real-time.
"""
import asyncio
import time
import logging
from typing import Dict, List, Optional, Any, Deque
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("novel-reader.progress_dashboard")


class TranslationJobStatus(Enum):
    """Status of translation jobs."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_RATE_LIMIT = "waiting_rate_limit"


class TranslationPriority(Enum):
    """Priority levels for translation jobs."""
    CRITICAL = "critical"  # Immediate translations
    HIGH = "high"          # Next chapters
    MEDIUM = "medium"      # Upcoming chapters
    LOW = "low"           # Batch translations
    BACKGROUND = "background"  # Pre-fetch, cache warming


@dataclass
class TranslationJob:
    """Represents a translation job in the dashboard."""
    job_id: str
    novel_id: str
    chapter: str
    source_text: str
    target_text: str
    source_language: str
    target_language: str
    priority: TranslationPriority
    status: TranslationJobStatus = TranslationJobStatus.PENDING
    added_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error_message: Optional[str] = None
    progress_percentage: float = 0.0
    estimated_completion_time: Optional[float] = None
    tokens_processed: int = 0
    total_tokens: Optional[int] = None
    model_used: Optional[str] = None
    quality_score: Optional[float] = None
    retry_count: int = 0


@dataclass
class ProgressStats:
    """Overall statistics for the progress dashboard."""
    total_jobs: int = 0
    pending_jobs: int = 0
    running_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    
    queued_jobs: int = 0
    active_workers: int = 0
    max_concurrent_workers: int = 5
    
    success_rate: float = 0.0
    average_processing_time: float = 0.0
    average_quality_score: float = 0.0
    
    jobs_by_priority: Dict[str, int] = field(default_factory=dict)
    jobs_by_model: Dict[str, int] = field(default_factory=dict)
    jobs_by_language_pair: Dict[str, int] = field(default_factory=dict)
    
    # Performance metrics
    tokens_per_minute: float = 0.0
    jobs_per_minute: float = 0.0
    api_calls_per_minute: float = 0.0


@dataclass
class SystemMetrics:
    """System-wide metrics for monitoring."""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    network_io: Dict[str, float] = field(default_factory=dict)
    
    active_connections: int = 0
    cache_hit_rate: float = 0.0
    rate_limit_hits: int = 0
    circuit_breaker_trips: int = 0


class RealTimeProgressDashboard:
    """
    Real-time translation progress dashboard with live updates and analytics.
    
    Provides comprehensive monitoring of translation jobs, performance metrics,
    and real-time status updates for both users and administrators.
    """
    
    def __init__(self, max_concurrent_jobs: int = 5):
        self.max_concurrent_jobs = max_concurrent_jobs
        self.jobs: Dict[str, TranslationJob] = {}
        self.stats = ProgressStats()
        self.system_metrics = SystemMetrics()
        self.job_history: Deque[TranslationJob] = deque(maxlen=1000)
        self.status_subscribers: List[asyncio.Queue] = []
        self.metrics_subscribers: List[asyncio.Queue] = []
        self.priority_queue: List[TranslationJob] = []
        self.active_workers: Dict[str, asyncio.Task] = {}
        
        # Performance tracking
        self.metrics_history: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self.performance_trends: Dict[str, List[float]] = defaultdict(list)
        
        # Start background metrics collection
        asyncio.create_task(self._collect_system_metrics())
    
    async def add_job(
        self,
        job_id: str,
        novel_id: str,
        chapter: str,
        source_text: str,
        target_text: str,
        source_language: str,
        target_language: str,
        priority: TranslationPriority = TranslationPriority.MEDIUM,
        estimated_tokens: Optional[int] = None,
        model: Optional[str] = None
    ) -> TranslationJob:
        """
        Add a new translation job to the dashboard.
        
        Args:
            job_id: Unique identifier for the job
            novel_id: ID of the novel being translated
            chapter: Chapter information
            source_text: Source text to translate
            target_text: Expected target text
            source_language: Source language code
            target_language: Target language code
            priority: Job priority
            estimated_tokens: Estimated token count
            model: Model to be used
            
        Returns:
            TranslationJob: The created job
        """
        job = TranslationJob(
            job_id=job_id,
            novel_id=novel_id,
            chapter=chapter,
            source_text=source_text,
            target_text=target_text,
            source_language=source_language,
            target_language=target_language,
            priority=priority,
            total_tokens=estimated_tokens,
            model_used=model,
            status=TranslationJobStatus.PENDING
        )
        
        self.jobs[job_id] = job
        self.stats.total_jobs += 1
        self.stats.queued_jobs += 1
        self.stats.jobs_by_priority[priority.value] += 1
        
        if model:
            self.stats.jobs_by_model[model] += 1
        
        language_pair = f"{source_language}→{target_language}"
        self.stats.jobs_by_language_pair[language_pair] += 1
        
        # Notify subscribers
        await self._notify_status_subscribers()
        
        # Log job addition
        logger.info(f"Added translation job {job_id} for novel {novel_id}, chapter {chapter}, priority {priority.value}")
        
        return job
    
    async def start_job(self, job_id: str):
        """
        Start processing a translation job.
        
        Args:
            job_id: ID of the job to start
        """
        job = self.jobs.get(job_id)
        if not job:
            logger.error(f"Attempted to start non-existent job {job_id}")
            return
        
        job.status = TranslationJobStatus.RUNNING
        job.started_at = time.time()
        self.stats.running_jobs += 1
        self.stats.queued_jobs -= 1
        
        # Update metrics
        self.stats.active_workers += 1
        
        await self._notify_status_subscribers()
        
        logger.info(f"Started translation job {job_id}")
    
    async def update_job_progress(
        self,
        job_id: str,
        progress: float,
        tokens_processed: int = 0,
        model_used: Optional[str] = None
    ):
        """
        Update the progress of a running job.
        
        Args:
            job_id: ID of the job
            progress: Progress percentage (0.0 to 1.0)
            tokens_processed: Number of tokens processed
            model_used: Model used for this step
        """
        job = self.jobs.get(job_id)
        if not job:
            return
        
        job.progress_percentage = progress
        job.tokens_processed += tokens_processed
        if model_used:
            job.model_used = model_used
        
        # Update estimated completion time
        if job.started_at:
            elapsed = time.time() - job.started_at
            if progress > 0:
                estimated_total_time = elapsed / progress
                job.estimated_completion_time = time.time() + (estimated_total_time - elapsed)
        
        await self._notify_status_subscribers()
    
    async def complete_job(
        self,
        job_id: str,
        success: bool = True,
        error_message: Optional[str] = None,
        quality_score: Optional[float] = None,
        model_used: Optional[str] = None
    ):
        """
        Complete a translation job.
        
        Args:
            job_id: ID of the job
            success: Whether the job completed successfully
            error_message: Error message if failed
            quality_score: Quality score of the translation
            model_used: Model that was used
        """
        job = self.jobs.get(job_id)
        if not job:
            return
        
        job.status = TranslationJobStatus.COMPLETED if success else TranslationJobStatus.FAILED
        job.completed_at = time.time()
        
        if not success:
            job.error_message = error_message
            self.stats.failed_jobs += 1
        else:
            self.stats.completed_jobs += 1
            if quality_score is not None:
                job.quality_score = quality_score
                # Update average quality score
                total_quality = self.stats.average_quality_score * (self.stats.completed_jobs - 1)
                self.stats.average_quality_score = (total_quality + quality_score) / self.stats.completed_jobs
        
        self.stats.running_jobs -= 1
        self.stats.active_workers -= 1
        
        # Move to history
        self.job_history.append(job)
        
        await self._notify_status_subscribers()
        
        logger.info(f"Completed translation job {job_id}, success: {success}")
    
    async def cancel_job(self, job_id: str):
        """
        Cancel a translation job.
        
        Args:
            job_id: ID of the job to cancel
        """
        job = self.jobs.get(job_id)
        if not job:
            return
        
        if job.status in (TranslationJobStatus.COMPLETED, TranslationJobStatus.FAILED, TranslationJobStatus.CANCELLED):
            return
        
        job.status = TranslationJobStatus.CANCELLED
        job.completed_at = time.time()
        
        self.stats.running_jobs -= 1
        self.stats.cancelled_jobs += 1
        
        # Remove from active workers if present
        if job_id in self.active_workers:
            task = self.active_workers.pop(job_id)
            task.cancel()
        
        await self._notify_status_subscribers()
        
        logger.info(f"Cancelled translation job {job_id}")
    
    async def get_dashboard_state(self) -> Dict[str, Any]:
        """
        Get the current state of the dashboard.
        
        Returns:
            Dict containing all dashboard state
        """
        return {
            'stats': {
                'total_jobs': self.stats.total_jobs,
                'pending_jobs': self.stats.pending_jobs,
                'running_jobs': self.stats.running_jobs,
                'completed_jobs': self.stats.completed_jobs,
                'failed_jobs': self.stats.failed_jobs,
                'cancelled_jobs': self.stats.cancelled_jobs,
                'queued_jobs': self.stats.queued_jobs,
                'active_workers': self.stats.active_workers,
                'max_concurrent_workers': self.stats.max_concurrent_workers,
                'success_rate': self.stats.success_rate,
                'average_processing_time': self.stats.average_processing_time,
                'average_quality_score': self.stats.average_quality_score,
            },
            'jobs_by_priority': dict(self.stats.jobs_by_priority),
            'jobs_by_model': dict(self.stats.jobs_by_model),
            'jobs_by_language_pair': dict(self.stats.jobs_by_language_pair),
            'system_metrics': {
                'cpu_usage': self.system_metrics.cpu_usage,
                'memory_usage': self.system_metrics.memory_usage,
                'disk_usage': self.system_metrics.disk_usage,
                'active_connections': self.system_metrics.active_connections,
                'cache_hit_rate': self.system_metrics.cache_hit_rate,
                'rate_limit_hits': self.system_metrics.rate_limit_hits,
                'circuit_breaker_trips': self.system_metrics.circuit_breaker_trips,
            },
            'active_jobs': {job_id: self._serialize_job(job) for job_id, job in self.jobs.items()},
        }
    
    def _serialize_job(self, job: TranslationJob) -> Dict[str, Any]:
        """Serialize a job for API responses."""
        return {
            'job_id': job.job_id,
            'novel_id': job.novel_id,
            'chapter': job.chapter,
            'source_language': job.source_language,
            'target_language': job.target_language,
            'priority': job.priority.value,
            'status': job.status.value,
            'added_at': job.added_at,
            'started_at': job.started_at,
            'completed_at': job.completed_at,
            'progress_percentage': job.progress_percentage,
            'estimated_completion_time': job.estimated_completion_time,
            'tokens_processed': job.tokens_processed,
            'total_tokens': job.total_tokens,
            'model_used': job.model_used,
            'quality_score': job.quality_score,
            'retry_count': job.retry_count,
            'error_message': job.error_message,
        }
    
    async def _notify_status_subscribers(self):
        """Notify all status subscribers of changes."""
        state = await self.get_dashboard_state()
        
        for subscriber_queue in self.status_subscribers:
            try:
                await subscriber_queue.put(state)
            except Exception:
                # Remove unhealthy subscribers
                self.status_subscribers.remove(subscriber_queue)
    
    async def _notify_metrics_subscribers(self):
        """Notify all metrics subscribers of changes."""
        metrics = {
            'system_metrics': self.system_metrics.__dict__,
            'stats': self.stats.__dict__,
            'performance_trends': dict(self.performance_trends),
        }
        
        for subscriber_queue in self.metrics_subscribers:
            try:
                await subscriber_queue.put(metrics)
            except Exception:
                # Remove unhealthy subscribers
                self.metrics_subscribers.remove(subscriber_queue)
    
    async def _collect_system_metrics(self):
        """Collect system metrics in the background."""
        while True:
            try:
                # Collect metrics (simplified - in real implementation, would use system monitoring)
                self.system_metrics.cpu_usage = 50.0 + (hash(time.time()) % 30)  # Simulated
                self.system_metrics.memory_usage = 60.0 + (hash(time.time() * 2) % 30)  # Simulated
                self.system_metrics.disk_usage = 70.0  # Static for demo
                
                # Update performance trends
                current_time = time.time()
                self.performance_trends['cpu'].append(self.system_metrics.cpu_usage)
                self.performance_trends['memory'].append(self.system_metrics.memory_usage)
                
                # Calculate average stats
                if self.stats.total_jobs > 0:
                    self.stats.success_rate = self.stats.completed_jobs / max(1, self.stats.completed_jobs + self.stats.failed_jobs)
                
                if self.stats.completed_jobs > 0:
                    completed_jobs_with_times = [
                        job for job in self.job_history if job.completed_at and job.started_at
                    ]
                    if completed_jobs_with_times:
                        avg_time = sum(
                            (job.completed_at - job.started_at) for job in completed_jobs_with_times
                        ) / len(completed_jobs_with_times)
                        self.stats.average_processing_time = avg_time
                
                # Update metrics history
                self.metrics_history.append({
                    'timestamp': current_time,
                    'stats': self.stats.__dict__,
                    'system_metrics': self.system_metrics.__dict__,
                })
                
                # Notify metrics subscribers
                await self._notify_metrics_subscribers()
                
                # Sleep before next collection
                await asyncio.sleep(5)  # Collect every 5 seconds
                
            except Exception as e:
                logger.error(f"Error collecting system metrics: {e}")
                await asyncio.sleep(10)  # Wait longer on error
    
    async def add_status_subscriber(self, subscriber_queue: asyncio.Queue):
        """Add a queue to receive status updates."""
        self.status_subscribers.append(subscriber_queue)
    
    async def add_metrics_subscriber(self, subscriber_queue: asyncio.Queue):
        """Add a queue to receive metrics updates."""
        self.metrics_subscribers.append(subscriber_queue)
    
    def get_job_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent job history.
        
        Args:
            limit: Maximum number of jobs to return
            
        Returns:
            List of serialized job history
        """
        return [self._serialize_job(job) for job in list(self.job_history)[-limit:]]
    
    def get_performance_trends(self, metric: str, time_window: int = 60) -> List[float]:
        """
        Get performance trends for a specific metric.
        
        Args:
            metric: Metric name (e.g., 'cpu', 'memory')
            time_window: Number of data points to return
            
        Returns:
            List of metric values
        """
        return list(self.performance_trends.get(metric, []))[-time_window:]
    
    async def cleanup_completed_jobs(self, max_age_minutes: int = 1440):
        """
        Clean up completed jobs that are older than specified age.
        
        Args:
            max_age_minutes: Maximum age in minutes for jobs to keep
        """
        current_time = time.time()
        cutoff_time = current_time - (max_age_minutes * 60)
        
        jobs_to_remove = []
        for job_id, job in self.jobs.items():
            if (job.status in (TranslationJobStatus.COMPLETED, TranslationJobStatus.FAILED, TranslationJobStatus.CANCELLED) and
                job.completed_at and job.completed_at < cutoff_time):
                jobs_to_remove.append(job_id)
        
        for job_id in jobs_to_remove:
            del self.jobs[job_id]
        
        if jobs_to_remove:
            logger.info(f"Cleaned up {len(jobs_to_remove)} old jobs")
    
    async def get_real_time_dashboard(self) -> Dict[str, Any]:
        """
        Get a real-time dashboard suitable for web display.
        
        Returns:
            Dashboard data with live updates
        """
        current_time = time.time()
        
        # Calculate real-time metrics
        active_jobs = [job for job in self.jobs.values() if job.status == TranslationJobStatus.RUNNING]
        queued_jobs = [job for job in self.jobs.values() if job.status == TranslationJobStatus.PENDING]
        
        # Get recent jobs for display
        recent_jobs = self.get_job_history(20)
        
        # Calculate performance indicators
        last_5_minutes = [m for m in self.metrics_history if m['timestamp'] > current_time - 300]
        
        return {
            'timestamp': current_time,
            'overview': {
                'total_jobs': self.stats.total_jobs,
                'success_rate': self.stats.success_rate,
                'average_processing_time': self.stats.average_processing_time,
                'average_quality_score': self.stats.average_quality_score,
                'active_workers': self.stats.active_workers,
                'max_workers': self.stats.max_concurrent_workers,
            },
            'current_status': {
                'running': self.stats.running_jobs,
                'queued': self.stats.queued_jobs,
                'completed': self.stats.completed_jobs,
                'failed': self.stats.failed_jobs,
            },
            'distribution': {
                'by_priority': self.stats.jobs_by_priority,
                'by_model': self.stats.jobs_by_model,
                'by_language_pair': self.stats.jobs_by_language_pair,
            },
            'system_health': {
                'cpu_usage': self.system_metrics.cpu_usage,
                'memory_usage': self.system_metrics.memory_usage,
                'disk_usage': self.system_metrics.disk_usage,
                'cache_hit_rate': self.system_metrics.cache_hit_rate,
                'rate_limit_hits': self.system_metrics.rate_limit_hits,
            },
            'recent_jobs': recent_jobs,
            'active_jobs': [self._serialize_job(job) for job in active_jobs],
            'queued_jobs': [self._serialize_job(job) for job in queued_jobs[:5]],  # Top 5 queued jobs
            'performance_trends': {
                'cpu': self.get_performance_trends('cpu', 30),
                'memory': self.get_performance_trends('memory', 30),
            },
        }


# Global dashboard instance
_progress_dashboard: Optional[RealTimeProgressDashboard] = None


def get_progress_dashboard(max_concurrent_jobs: int = 5) -> RealTimeProgressDashboard:
    """Get or create the global progress dashboard."""
    global _progress_dashboard
    if _progress_dashboard is None:
        _progress_dashboard = RealTimeProgressDashboard(max_concurrent_jobs)
    return _progress_dashboard


def add_dashboard_status_subscriber(subscriber_queue: asyncio.Queue):
    """Add a status subscriber to the global dashboard."""
    dashboard = get_progress_dashboard()
    asyncio.create_task(dashboard.add_status_subscriber(subscriber_queue))


def add_dashboard_metrics_subscriber(subscriber_queue: asyncio.Queue):
    """Add a metrics subscriber to the global dashboard."""
    dashboard = get_progress_dashboard()
    asyncio.create_task(dashboard.add_metrics_subscriber(subscriber_queue))


def cleanup_dashboard_jobs(max_age_minutes: int = 1440):
    """Clean up old jobs in the global dashboard."""
    dashboard = get_progress_dashboard()
    asyncio.create_task(dashboard.cleanup_completed_jobs(max_age_minutes))