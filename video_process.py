import numpy as np
import cv2,time,argparse,os,queue,torch

from deep_sort.utils.parser import get_config
from deep_sort.deep_sort import DeepSort

class Frame_Diff:
    def __init__(self,cap,t,resize) -> None:
        super().__init__()
        self.t_Frames=[]
        self.cap=cap
        self.t=t
        self.resize=resize
        for _ in range(t):
            ret,frame=cap.read()
            if ret:
                frame=frame/255.0
                self.shape=(frame.shape[1],frame.shape[0]) if self.resize==1.0 else (int(self.resize*frame.shape[1]),int(self.resize*frame.shape[0]))
                frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
                self.t_Frames.append(frame)

    def update(self):
        ret,frame=self.cap.read()
        gradient=np.zeros(self.t_Frames[-1].shape)
        if ret:
            frame=frame/255.0
            frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
            gradient=frame-self.t_Frames[0]
            self.t_Frames.append(frame)
            self.t_Frames.pop(0)
            gradient=np.maximum(gradient,0)
            gradient=gradient/(gradient[np.unravel_index(gradient.argmax(), gradient.shape)])
            gradient=gradient*255
        return ret,gradient.astype(np.uint8)

class Motion_History:
    def __init__(self,cap,t,resize):
        self.tau=200
        self.delta=20
        self.xi=20
        self.t=t
        self.resize=resize
        self.cap=cap
        self.data = queue.Queue()
        ret,frame=cap.read()
        if ret:
            self.shape=(frame.shape[1],frame.shape[0]) if self.resize==1.0 else (int(self.resize*frame.shape[1]),int(self.resize*frame.shape[0]))
            frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
            for i in range(self.t):
                self.data.put(frame)
        self.H = np.zeros(frame.shape)  
        
    def update(self):        
        ret,frame=cap.read()
        if not ret:
            return ret,frame
        frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
        self.data.put(frame)
        old_frame=self.data.get()        
        a=cv2.addWeighted(old_frame.astype(float), 1, frame.astype(float), -1, 0)
        D= np.fabs(a)
        Psi= D >=self.xi
        c=self.H-self.delta
        H=np.maximum(0,c)
        H[Psi]=self.tau
        self.H=H
        return ret,H.astype("uint8")
    
class Motion_Detect:
    def __init__(self,cap,t,resize) -> None:
        super().__init__()
        self.t_Frames=[]
        self.cap=cap
        self.t=t
        self.resize=resize
        ret,frame=cap.read()
        if ret:
            self.shape=(frame.shape[1],frame.shape[0]) if self.resize==1.0 else (int(self.resize*frame.shape[1]),int(self.resize*frame.shape[0]))
        cfg = get_config()
        cfg.merge_from_file('deep_sort/configs/deep_sort.yaml')
        self.deepsort = DeepSort(cfg.DEEPSORT.REID_CKPT, 
                            max_dist=cfg.DEEPSORT.MAX_DIST, min_confidence=cfg.DEEPSORT.MIN_CONFIDENCE, 
                            nms_max_overlap=cfg.DEEPSORT.NMS_MAX_OVERLAP, max_iou_distance=cfg.DEEPSORT.MAX_IOU_DISTANCE, 
                            max_age=cfg.DEEPSORT.MAX_AGE, n_init=cfg.DEEPSORT.N_INIT, nn_budget=cfg.DEEPSORT.NN_BUDGET, 
                            use_cuda=True, use_appearence=False)
        self.bs = cv2.createBackgroundSubtractorKNN(detectShadows=True,history=150,dist2Threshold=700)
        self.bs.setNSamples(6)
        
            
    def compute_color_for_labels(self,label):
        """
        Simple function that adds fixed color depending on the class
        """
        palette = (2 ** 11 - 1, 2 ** 15 - 1, 2 ** 20 - 1)
        color = [int((p * (label ** 2 - label + 1)) % 255) for p in palette]
        return tuple(color)
        
    def draw_boxes(self,img, bbox, labels=None, identities=None, Vx=None, Vy=None):
        for i, box in enumerate(bbox):
            xmin, ymin, xmax, ymax = [int(i) for i in box]
            ymin = min(img.shape[0]-5,max(5,ymin))
            xmin = min(img.shape[1]-5,max(5,xmin))
            ymax = max(5,min(img.shape[0]-5,ymax))
            xmax = max(5,min(img.shape[1]-5,xmax))
            # box text and bar
            id = int(identities[i]) if identities is not None else 0
            color = self.compute_color_for_labels(id)
            label=labels[i] if labels is not None else 0
            vx=Vx[i] if Vx is not None else 0
            vy=Vy[i] if Vy is not None else 0
            info = '{:d}'.format(id)
            t_size=cv2.getTextSize(info, cv2.FONT_HERSHEY_TRIPLEX, 0.4 , 1)[0]
            cv2.rectangle(img, (xmin, ymin), (xmax, ymax), color, 2)
            cv2.rectangle(img, (xmin, ymin), (xmin + t_size[0]+2, ymin + t_size[1]+4), color, -1)
            cv2.putText(img, info, (xmin+1, ymin+t_size[1]+1), cv2.FONT_HERSHEY_TRIPLEX, 0.4, [255,255,255], 1)
        return img
    
    def update(self):
        grabbed, frame = cap.read()
        if grabbed:
            frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
            guss_frame=cv2.GaussianBlur(frame, ksize=(5,5), sigmaX=0, sigmaY=0)
            fgmask = self.bs.apply(guss_frame) 
            th = cv2.threshold(fgmask.copy(), 100, 255, cv2.THRESH_BINARY)[1]
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            result=cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(result, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE) # 该函数计算一幅图像中目标的轮廓
            xywhs=[]
            confs=[]
            labels=[]
            for c in contours:
                max_area=self.shape[0]*self.shape[1]
                if 0.3*max_area > cv2.contourArea(c) > 0.00001*max_area:
                    (x, y, w, h) = cv2.boundingRect(c)
                    if 0.2 <w/h < 5:
                        xywhs.append([x+w/2,y+h/2,w,h])
                        confs.append(0.5)
                        labels.append(int(1))
            if len(xywhs)>0:
                xywhs = torch.Tensor(xywhs)
                confs = torch.Tensor(confs)
                outputs = self.deepsort.update(xywhs, confs , labels, frame)
                if len(outputs) > 0:
                    bbox_xyxy = outputs[:, :4]
                    labels = outputs[:, 4]
                    identities = outputs[:, 5]
                    frame=self.draw_boxes(frame, bbox_xyxy, identities=identities)
        return grabbed,frame

class Dynamic_Img:
    def __init__(self,cap,t,resize) -> None:
        super().__init__()
        self.t_Frames=[]
        self.cap=cap
        self.t=t
        self.resize=resize
        for _ in range(t):
            ret,frame=cap.read()
            if ret:
                self.shape=(frame.shape[1],frame.shape[0]) if self.resize==1.0 else (int(self.resize*frame.shape[1]),int(self.resize*frame.shape[0]))
                frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
                frame=frame*(1/255)
                self.t_Frames.append(frame)

    def update(self):
        ret,frame=self.cap.read()
        dimg=np.zeros(self.t_Frames[-1].shape)
        if ret:
            frame=frame if self.resize==1.0 else cv2.resize(frame,self.shape)
            frame=frame*(1/255)
            for i in range(1,len(self.t_Frames)+1):
                temp=0
                for j in range(i,len(self.t_Frames)+1):
                    temp+=(2*j-self.t-1)/(j)     
                
                dimg+=temp*self.t_Frames[i-1]
            self.t_Frames.append(frame)
            self.t_Frames.pop(0)
            
            dimg=np.maximum(dimg,0)
            dimg=dimg/(dimg[np.unravel_index(dimg.argmax(), dimg.shape)])
            dimg=255*dimg
        return ret,dimg.astype(np.uint8)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-j','--job_name', type=int, default='0', help='selective job name list:dynamic_img,motion_detect,motion_history,frame_diff')
    parser.add_argument('-t','--t-length', type=int, default='10', help='number of multi-frames')
    parser.add_argument('-v','--video_path', type=str, default='input/videos/car.mp4', help='video or stream path')
    parser.add_argument('-r','--resize', type=float, default='1', help='img resize retio')
    parser.add_argument('-s',"--save_result", action='store_true',help='if save result to video')
    args = parser.parse_args()
    
    selector={0:Motion_Detect,1:Dynamic_Img,2:Motion_History,3:Frame_Diff}
    args.video_path=0 if args.video_path=='0' else args.video_path
    cap = cv2.VideoCapture(args.video_path)
    video_process=selector[args.job_name](cap,args.t_length,args.resize)
    fourcc = cv2.VideoWriter_fourcc('P','I','M','1')
    if args.save_result:
        save_path='camera_result.avi' if args.video_path=='0' else os.path.join(os.path.dirname(args.video_path),
                                                                os.path.basename(args.video_path).split('.')[0]+'_VPR.avi')
        video = cv2.VideoWriter(save_path, fourcc, 25 ,video_process.shape)
    while cap.isOpened():
        ret,dimg=video_process.update()
        cv2.imshow('dynamic',dimg)
        if args.save_result:
            video.write(dimg)
        time.sleep(0)
        if cv2.waitKey(1) == ord('q'):
            break
    cap.release()
    if args.save_result:
        video.release()
    cv2.destroyAllWindows()