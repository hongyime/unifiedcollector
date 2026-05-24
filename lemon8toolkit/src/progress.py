"""
Unified Lemon8 Toolkit - Progress Management
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional

import config

class ProgressManager:
    def __init__(self, auto_save: bool = True):
        config.ensure_data_directory()
        self.progress_data: Dict[str, Any] = self._load_progress()
        self.auto_save = auto_save
    
    def _load_progress(self) -> Dict[str, Any]:
        """Load progress data from file"""
        try:
            if os.path.exists(config.DOWNLOAD_PROGRESS_FILE):
                with open(config.DOWNLOAD_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {'sessions': [], 'current_session': None}
        except Exception as e:
            print(f"⚠️ Error loading progress file: {e}")
            return {'sessions': [], 'current_session': None}
    
    def _save_progress(self):
        """Save progress data to file"""
        self._prune_sessions()
        try:
            tmp = config.DOWNLOAD_PROGRESS_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, config.DOWNLOAD_PROGRESS_FILE)
        except (OSError, TypeError) as e:
            print(f"⚠️ Error saving progress file: {e}")

    def _prune_sessions(self):
        """Keep only the last 100 sessions to prevent unbounded growth."""
        sessions = self.progress_data.get('sessions', [])
        if len(sessions) > 100:
            self.progress_data['sessions'] = sessions[-100:]
    
    def start_session(self, session_type: str, identifier: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Start a new scraping session
        
        Args:
            session_type: 'user', 'feed', or 'tag'
            identifier: Username, 'foryou', or tag_id
            metadata: Optional session metadata
            
        Returns:
            Session ID
        """
        session_id = f"{session_type}_{identifier}_{int(datetime.now().timestamp())}"
        
        session_data = {
            'session_id': session_id,
            'session_type': session_type,
            'identifier': identifier,
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'status': 'in_progress',
            'scraped_media': [],
            'downloaded_media': [],
            'failed_downloads': [],
            'total_scraped': 0,
            'total_downloaded': 0,
            'total_failed': 0,
            'metadata': metadata or {}
        }
        
        # Add to sessions list
        self.progress_data['sessions'].append(session_data)
        self.progress_data['current_session'] = session_id
        
        if self.auto_save:
            self.save()
        
        print(f"📝 Started session: {session_id}")
        return session_id
    
    def save(self):
        """Force save progress data to file"""
        self._save_progress()
    
    def update_session_scraped_media(self, session_id: str, media_urls: List[str]):
        """Update scraped media for a session"""
        session = self._get_session(session_id)
        if session:
            session['scraped_media'].extend(media_urls)
            session['total_scraped'] = len(session['scraped_media'])
            session['last_updated'] = datetime.now().isoformat()
            if self.auto_save:
                self.save()
    
    def update_session_downloaded_media(self, session_id: str, media_url: str, file_path: str):
        """Update downloaded media for a session"""
        session = self._get_session(session_id)
        if session:
            download_info = {
                'url': media_url,
                'file_path': file_path,
                'download_time': datetime.now().isoformat()
            }
            session['downloaded_media'].append(download_info)
            session['total_downloaded'] = len(session['downloaded_media'])
            session['last_updated'] = datetime.now().isoformat()
            if self.auto_save:
                self.save()
    
    def update_session_failed_download(self, session_id: str, media_url: str, error: str):
        """Update failed download for a session"""
        session = self._get_session(session_id)
        if session:
            failure_info = {
                'url': media_url,
                'error': error,
                'failure_time': datetime.now().isoformat()
            }
            session['failed_downloads'].append(failure_info)
            session['total_failed'] = len(session['failed_downloads'])
            session['last_updated'] = datetime.now().isoformat()
            if self.auto_save:
                self.save()
    
    def end_session(self, session_id: str, status: str = 'completed'):
        """
        End a scraping session
        
        Args:
            session_id: Session ID to end
            status: 'completed', 'failed', or 'cancelled'
        """
        session = self._get_session(session_id)
        if session:
            session['end_time'] = datetime.now().isoformat()
            session['status'] = status
            session['last_updated'] = datetime.now().isoformat()
            
            # Clear current session if this was it
            if self.progress_data.get('current_session') == session_id:
                self.progress_data['current_session'] = None
            
            if self.auto_save:
                self.save()
            print(f"🏁 Ended session: {session_id} ({status})")
    
    def _get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID"""
        for session in self.progress_data['sessions']:
            if session['session_id'] == session_id:
                return session
        return None
    
    def get_current_session(self) -> Optional[Dict[str, Any]]:
        """Get the current active session"""
        current_id = self.progress_data.get('current_session')
        if current_id:
            return self._get_session(current_id)
        return None
    
    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get summary of a session"""
        session = self._get_session(session_id)
        if not session:
            return None
        
        return {
            'session_id': session['session_id'],
            'session_type': session['session_type'],
            'identifier': session['identifier'],
            'status': session['status'],
            'start_time': session['start_time'],
            'end_time': session.get('end_time'),
            'duration': self._calculate_duration(session),
            'total_scraped': session['total_scraped'],
            'total_downloaded': session['total_downloaded'],
            'total_failed': session['total_failed'],
            'success_rate': self._calculate_success_rate(session)
        }
    
    def _calculate_duration(self, session: Dict[str, Any]) -> Optional[str]:
        """Calculate session duration"""
        try:
            start = datetime.fromisoformat(session['start_time'])
            end_time = session.get('end_time')
            if end_time:
                end = datetime.fromisoformat(end_time)
            else:
                end = datetime.now()
            
            duration = end - start
            return str(duration).split('.')[0]  # Remove microseconds
        except Exception:
            return None
    
    def _calculate_success_rate(self, session: Dict[str, Any]) -> float:
        """Calculate download success rate"""
        total_attempts = session['total_downloaded'] + session['total_failed']
        if total_attempts == 0:
            return 0.0
        return (session['total_downloaded'] / total_attempts) * 100
    
    def get_all_sessions_summary(self) -> List[Dict[str, Any]]:
        """Get summary of all sessions"""
        summaries = []
        for session in self.progress_data['sessions']:
            summary = self.get_session_summary(session['session_id'])
            if summary:
                summaries.append(summary)
        return summaries
    
    def get_stats(self) -> Dict[str, Any]:
        """Get overall progress statistics"""
        total_sessions = len(self.progress_data['sessions'])
        completed_sessions = sum(1 for s in self.progress_data['sessions'] if s['status'] == 'completed')
        in_progress_sessions = sum(1 for s in self.progress_data['sessions'] if s['status'] == 'in_progress')
        
        total_scraped = sum(s['total_scraped'] for s in self.progress_data['sessions'])
        total_downloaded = sum(s['total_downloaded'] for s in self.progress_data['sessions'])
        total_failed = sum(s['total_failed'] for s in self.progress_data['sessions'])
        
        return {
            'total_sessions': total_sessions,
            'completed_sessions': completed_sessions,
            'in_progress_sessions': in_progress_sessions,
            'total_media_scraped': total_scraped,
            'total_media_downloaded': total_downloaded,
            'total_failed_downloads': total_failed,
            'overall_success_rate': (total_downloaded / max(total_downloaded + total_failed, 1)) * 100,
            'current_session': self.progress_data.get('current_session')
        }
    
    def resume_session(self, session_id: str) -> bool:
        """Resume an existing session"""
        session = self._get_session(session_id)
        if session and session['status'] == 'in_progress':
            self.progress_data['current_session'] = session_id
            self._save_progress()
            print(f"▶️ Resumed session: {session_id}")
            return True
        else:
            print(f"❌ Cannot resume session: {session_id} (not found or not in progress)")
            return False
    
    def clear_progress_history(self):
        """Clear all progress history"""
        self.progress_data = {'sessions': [], 'current_session': None}
        self._save_progress()
        print("🗑️ Progress history cleared")